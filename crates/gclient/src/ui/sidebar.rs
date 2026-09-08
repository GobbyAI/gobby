// upstream: herdr v0.8.0 src/ui/sidebar.rs
//! Sidebar: project cards on top, agent rows below, collapsed rail.
//!
//! herdr geometry kept as is: a `│` separator column on the right, a
//! two-row projects header with a footer row for the `«` toggle, a
//! three-row agents header (rule + title), and the collapsed rail split in
//! half around a `─` divider. The projects section itself is `projects`.

pub mod agents;
pub mod projects;

use crate::theme::Palette;
use crate::ui::chrome::{Chrome, Mode, SidebarState, WorkspaceView};
use crate::ui::hit::SidebarSection;
use crate::ui::sidebar_rows::{project_rows, RowKind, SidebarRow};
use crate::ui::status::state_dot;
use gobby_terminal::layout::ScrollMetrics;
use ratatui::layout::Rect;
use ratatui::style::{Color, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Paragraph};
use ratatui::Frame;

pub use agents::{
    agent_blocked, agent_label, agent_rows, attention_order, machine_filter_label,
    next_machine_filter, AGENTS_HEADER_ROWS, ALL_MACHINES,
};
pub use projects::{project_list_metrics, projects_body_rect};

/// Projects share of the sidebar when `SidebarState::section_split` is unset.
const DEFAULT_SECTION_SPLIT: f32 = 0.5;

#[derive(Debug, Clone, Default)]
pub struct SidebarHits {
    /// Project cards, by project id (both lines of the card).
    pub projects: Vec<(String, Rect)>,
    /// Worktree rows, by worktree id.
    pub worktrees: Vec<(String, Rect)>,
    /// The `▸`/`▾` cell of each card that has worktrees, by project id.
    pub group_toggles: Vec<(String, Rect)>,
    pub projects_new: Option<Rect>,
    pub projects_menu: Option<Rect>,
    /// Scrollbar lane beside the projects, when one was drawn.
    pub projects_scrollbar: Option<Rect>,
    /// Agent rows, by attention entry id.
    pub agents: Vec<(String, Rect)>,
    /// Scrollbar lane beside the agents, when one was drawn.
    pub agents_scrollbar: Option<Rect>,
    /// The machine filter label of the agents header, when more than one
    /// machine is known.
    pub machine_filter: Option<Rect>,
    /// The `grouped`/`priority` sort label of the agents header, when the
    /// mouse is captured.
    pub agent_sort: Option<Rect>,
    /// The `«`/`»` collapse toggle cell.
    pub toggle: Option<Rect>,
}

/// Screen row of the `─` rule between the projects and agents sections,
/// when the sidebar draws one (the collapsed rail's divider or the agents
/// header's rule).
pub fn section_divider_y(area: Rect, sidebar: &SidebarState) -> Option<u16> {
    if sidebar.collapsed {
        return collapsed_sections(area).1;
    }
    let (_, agents) = expanded_sections(area, sidebar.section_split);
    (agents.width > 0 && agents.height >= AGENTS_HEADER_ROWS).then_some(agents.y)
}

/// Expanded sidebar; returns the row hit areas.
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

    let (projects_area, agents_area) = expanded_sections(area, chrome.sidebar.section_split);
    projects::render_projects(frame, projects_area, ws, chrome, is_navigating, &mut hits);
    agents::render_agents(frame, agents_area, ws, chrome, &mut hits);
    hits.toggle = render_toggle(frame, expanded_toggle_rect(area), "«", p);
    hits
}

/// Collapsed rail (`COLLAPSED_WIDTH` columns): state dots and indexes only.
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

    let (projects_area, divider_y, agents_area) = collapsed_sections(area);
    if projects_area == Rect::default() {
        hits.toggle = render_toggle(frame, collapsed_toggle_rect(area), "»", p);
        return hits;
    }

    let cards = project_rows(ws, chrome)
        .into_iter()
        .filter(|row| row.kind == RowKind::Project);
    for (index, row) in cards.enumerate() {
        let y = projects_area.y + index as u16;
        if y >= projects_area.y + projects_area.height {
            break;
        }
        let (icon, icon_color) = state_dot(row.state, p);
        let selected = row.selected && is_navigating;
        let active = row.active;
        let (row_style, num_style) = if selected {
            (
                Style::default().bg(p.surface1),
                Style::default().fg(p.overlay1).bg(p.surface1),
            )
        } else if active {
            (
                Style::default().bg(p.surface_dim),
                Style::default().fg(p.text).bg(p.surface_dim),
            )
        } else {
            (Style::default(), Style::default().fg(p.overlay0))
        };
        let rect = Rect::new(projects_area.x, y, projects_area.width, 1);
        frame.render_widget(
            Paragraph::new(Line::from(vec![
                // herdr pads single digits and keeps the dot at column 2 for
                // two-digit positions (`10·`) instead of clipping it.
                Span::styled(format!("{:<2}", index + 1), num_style),
                Span::styled(icon, Style::default().fg(icon_color)),
            ]))
            .style(row_style),
            rect,
        );
        hits.projects.push((row.id, rect));
    }

    if let Some(divider_y) = divider_y {
        let buf = frame.buffer_mut();
        for x in projects_area.x..projects_area.x + projects_area.width {
            buf[(x, divider_y)].set_symbol("─");
            buf[(x, divider_y)].set_style(Style::default().fg(p.surface_dim));
        }
    }

    let content = Rect::new(
        agents_area.x,
        agents_area.y,
        agents_area.width,
        agents_area.height.saturating_sub(1),
    );
    if content != Rect::default() {
        for (index, row) in agent_rows(ws, chrome).iter().enumerate() {
            let y = content.y + index as u16;
            if y >= content.y + content.height {
                break;
            }
            let (icon, icon_color) = state_dot(row.state, p);
            let rect = Rect::new(content.x, y, content.width, 1);
            frame.render_widget(
                Paragraph::new(Line::from(vec![
                    Span::styled(format!("{:<2}", index + 1), Style::default().fg(p.overlay0)),
                    Span::styled(icon, Style::default().fg(icon_color)),
                ])),
                rect,
            );
            hits.agents.push((row.id.clone(), rect));
        }
    }

    hits.toggle = render_toggle(frame, collapsed_toggle_rect(area), "»", p);
    hits
}

/// herdr `workspace_list_scroll_metrics` for fixed one-line rows.
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

/// Scroll metrics of one list section as the last frame laid it out: the
/// rows it holds against the body rows `chrome.view.sidebar_rect` gives it.
/// The mouse reads these to scroll the list the renderer will draw next.
pub fn section_metrics<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    section: SidebarSection,
) -> ScrollMetrics {
    let (projects, agents) =
        expanded_sections(chrome.view.sidebar_rect, chrome.sidebar.section_split);
    match section {
        SidebarSection::Projects => {
            let heights: Vec<u16> = project_rows(ws, chrome)
                .iter()
                .map(SidebarRow::height)
                .collect();
            project_list_metrics(
                &heights,
                projects_body_rect(projects, false).height,
                chrome.sidebar.scroll,
            )
        }
        SidebarSection::Agents => {
            let heights: Vec<u16> = agent_rows(ws, chrome)
                .iter()
                .map(SidebarRow::height)
                .collect();
            project_list_metrics(
                &heights,
                agents_body_rect(agents, false).height,
                chrome.sidebar.agents_scroll,
            )
        }
    }
}

fn scrollbar_track(area: Rect, body: Rect) -> Rect {
    Rect::new(
        area.x + area.width.saturating_sub(1),
        body.y,
        1,
        body.height,
    )
}

/// herdr `sidebar_section_heights`; `split` overrides the ratio with an
/// explicit projects row count, clamped the same way.
fn section_heights(total_h: u16, split: Option<u16>) -> (u16, u16) {
    if total_h == 0 {
        return (0, 0);
    }
    if total_h < 6 {
        let projects_h = total_h.div_ceil(2);
        return (projects_h, total_h.saturating_sub(projects_h));
    }
    let projects_h =
        split.unwrap_or_else(|| (f32::from(total_h) * DEFAULT_SECTION_SPLIT).round() as u16);
    let projects_h = projects_h.clamp(3, total_h.saturating_sub(3));
    (projects_h, total_h.saturating_sub(projects_h))
}

/// herdr `expanded_sidebar_sections`: content excludes the separator column.
pub fn expanded_sections(area: Rect, split: Option<u16>) -> (Rect, Rect) {
    let content = Rect::new(area.x, area.y, area.width.saturating_sub(1), area.height);
    if content.width == 0 || content.height == 0 {
        return (Rect::default(), Rect::default());
    }
    let (projects_h, agents_h) = section_heights(content.height, split);
    (
        Rect::new(content.x, content.y, content.width, projects_h),
        Rect::new(content.x, content.y + projects_h, content.width, agents_h),
    )
}

/// herdr `collapsed_sidebar_sections`: projects, divider row, agents.
pub fn collapsed_sections(area: Rect) -> (Rect, Option<u16>, Rect) {
    let content = Rect::new(area.x, area.y, area.width.saturating_sub(1), area.height);
    if content.width == 0 || content.height == 0 {
        return (Rect::default(), None, Rect::default());
    }
    if content.height < 7 {
        return (content, None, Rect::default());
    }
    let projects_h = content.height.div_ceil(2);
    let agents_h = content.height.saturating_sub(projects_h + 1);
    if agents_h == 0 {
        return (content, None, Rect::default());
    }
    let divider_y = content.y + projects_h;
    (
        Rect::new(content.x, content.y, content.width, projects_h),
        Some(divider_y),
        Rect::new(content.x, divider_y + 1, content.width, agents_h),
    )
}

/// herdr `agent_panel_body_rect`.
pub fn agents_body_rect(area: Rect, has_scrollbar: bool) -> Rect {
    if area.width == 0 || area.height <= AGENTS_HEADER_ROWS {
        return Rect::default();
    }
    let body_y = area.y + AGENTS_HEADER_ROWS;
    Rect::new(
        area.x,
        body_y,
        area.width.saturating_sub(u16::from(has_scrollbar)),
        (area.y + area.height).saturating_sub(body_y),
    )
}

/// herdr `expanded_sidebar_toggle_rect`.
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

/// herdr `collapsed_sidebar_toggle_rect`.
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

/// Draw the collapse toggle and return its cell, if there was room for one.
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
    let sep_x = area.x + area.width.saturating_sub(1);
    let buf = frame.buffer_mut();
    for y in area.y..area.y + area.height {
        buf[(sep_x, y)].set_symbol("│");
        buf[(sep_x, y)].set_style(Style::default().fg(color));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::Workspace;
    use crate::daemon::{Checkout, ProjectRow, SidebarRows, SourceStatus, WorktreeRow};
    use crate::ui::scrollbar::should_show_scrollbar;
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
    fn section_heights_follow_herdr_clamps() {
        assert_eq!(section_heights(0, None), (0, 0));
        assert_eq!(section_heights(5, None), (3, 2));
        assert_eq!(section_heights(40, None), (20, 20));
        assert_eq!(section_heights(40, Some(30)), (30, 10));
        assert_eq!(section_heights(40, Some(50)), (37, 3));
        assert_eq!(section_heights(40, Some(0)), (3, 37));
    }

    #[test]
    fn expanded_sidebar_lists_projects_and_agents_with_hits() {
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
            " projects",
            "● alpha",
            "   main ↑2",
            "feature · #123",
            "○ beta",
            " agents",
            "term-alpha",
            "blocked",
            " new",
            "menu│",
        ] {
            assert!(text.contains(needle), "missing {needle:?}:\n{text}");
        }
        assert!(!text.contains('!'), "{text}");
        assert_eq!(
            hits.projects,
            vec![
                ("proj-alpha".to_string(), Rect::new(0, 2, 25, 2)),
                ("proj-beta".to_string(), Rect::new(0, 5, 25, 2)),
            ]
        );
        assert_eq!(
            hits.worktrees,
            vec![("wt-1".to_string(), Rect::new(0, 4, 25, 1))]
        );
        assert_eq!(
            hits.group_toggles,
            vec![("proj-alpha".to_string(), Rect::new(24, 2, 1, 1))]
        );
        assert_eq!(hits.projects_new, Some(Rect::new(0, 19, 4, 1)));
        assert_eq!(hits.projects_menu, Some(Rect::new(21, 19, 4, 1)));
        assert_eq!(
            hits.agents,
            vec![("run:term-alpha".to_string(), Rect::new(0, 23, 25, 2))]
        );
        let lines: Vec<&str> = text.lines().collect();
        assert!(lines[2].ends_with("▾│"), "{:?}", lines[2]);
        assert!(lines[4].starts_with("▸  └─ ○ feature"), "{:?}", lines[4]);
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
        assert_eq!(lines[0], "1 ●│");
        assert_eq!(lines[1], "2 ○│");
        assert_eq!(lines[2], "   │");
        assert_eq!(lines[6], "───│");
        assert_eq!(lines[7], "1 ●│");
        assert_eq!(lines[11], " » │");
        assert_eq!(hits.projects.len(), 2);
        assert!(hits.worktrees.is_empty());
        assert_eq!(hits.agents.len(), 1);
    }

    #[test]
    fn agents_scroll_clamps_to_the_last_page() {
        let metrics = list_metrics(10, 4, 99);
        assert_eq!(metrics.max_offset_from_bottom, 6);
        assert_eq!(metrics.offset_from_bottom, 0);
        assert_eq!(metrics.viewport_rows, 4);
        assert!(!should_show_scrollbar(list_metrics(3, 4, 0)));
    }
}
