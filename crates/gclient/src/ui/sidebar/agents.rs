// upstream: herdr v0.8.0 src/client/shell/agent_sidebar.rs
//! The agents section: one two-line row per agent the machine filter admits,
//! in tab order or by urgency (`agent_sort`), under a three-row header that
//! carries the filter and sort labels.
//!
//! herdr lists every workspace's agents and marks the view with a label in
//! the header; gclient's filter is the machine axis instead: the focused
//! project's local agents by default, one remote machine, or every agent of
//! every project and machine (`ALL_MACHINES`).

use std::cmp::Reverse;

use super::{agents_body_rect, project_list_metrics, scrollbar_track, SidebarHits};
use crate::app::project_tabs::TabSet;
use crate::app::short_terminal_id;
use crate::app::sidebar_model::{agent_row_state, urgency, AgentEntry, SidebarModel};
use crate::ui::chrome::{Chrome, RowState, WorkspaceView};
use crate::ui::scrollbar::{render_scrollbar, should_show_scrollbar};
use crate::ui::settings::AgentSort;
use crate::ui::sidebar_rows::{row_line, row_second_line, RowKind, SidebarRow};
use crate::ui::text::display_width;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::Span;
use ratatui::widgets::Paragraph;
use ratatui::Frame;

/// herdr `AGENT_PANEL_HEADER_ROWS`: the rule, the title row, one blank.
pub const AGENTS_HEADER_ROWS: u16 = 3;
/// `SidebarState::machine_filter` value that admits every agent.
pub const ALL_MACHINES: &str = "all";
/// Header label of the default filter.
const LOCAL_LABEL: &str = "local";

/// What the row calls the agent: its name with the session ref (`planner
/// #12217`) when a session row joined, else with the tmux address that keeps
/// two same-named terminals apart, never a raw UUID.
pub fn agent_label<W: WorkspaceView>(ws: &W, agent: &AgentEntry) -> String {
    if let Some(reference) = agent.session_ref.as_deref() {
        return format!("{} {reference}", agent.name);
    }
    let address = ws
        .pane_for_terminal(&agent.terminal_id)
        .and_then(|pane| ws.pane(pane).address.clone())
        .filter(|address| *address != agent.name);
    match address {
        Some(address) => format!("{} {address}", agent.name),
        None => agent.name.clone(),
    }
}

/// The filter after `current` in the cycle `local -> all -> each remote
/// machine -> local`; a machine the model no longer knows wraps to local.
pub fn next_machine_filter(model: &SidebarModel, current: Option<&str>) -> Option<String> {
    let remote: Vec<&String> = model
        .machines
        .iter()
        .filter(|machine| **machine != model.local_machine)
        .collect();
    match current {
        None => Some(ALL_MACHINES.to_string()),
        Some(ALL_MACHINES) => remote.first().map(|machine| machine.to_string()),
        Some(machine) => remote
            .iter()
            .position(|candidate| *candidate == machine)
            .and_then(|index| remote.get(index + 1))
            .map(|machine| machine.to_string()),
    }
}

/// Header text of a filter: `local`, `all`, or the machine's short id.
pub fn machine_filter_label(current: Option<&str>) -> &str {
    match current {
        None => LOCAL_LABEL,
        Some(ALL_MACHINES) => ALL_MACHINES,
        Some(machine) => short_terminal_id(machine),
    }
}

/// The agent's state as the chrome shows it: the pane's live flags when one
/// shows the terminal, else what the roster said.
fn agent_state<W: WorkspaceView>(ws: &W, agent: &AgentEntry) -> RowState {
    ws.pane_for_terminal(&agent.terminal_id)
        .map_or(agent.state, |pane| agent_row_state(agent, ws.pane(pane)))
}

/// Whether a jump to `entry_id` should open the response dialog: the agent
/// waits on attention, or no agent row backs the entry (a prompt-only entry).
pub fn agent_blocked<W: WorkspaceView>(ws: &W, entry_id: &str) -> bool {
    ws.sidebar()
        .agents
        .iter()
        .find(|agent| agent.entry_id == entry_id)
        .is_none_or(|agent| agent_state(ws, agent) == RowState::Attention)
}

fn admits<W: WorkspaceView>(ws: &W, chrome: &Chrome, agent: &AgentEntry) -> bool {
    let same_project = ws
        .focused_project()
        .is_none_or(|focused| agent.project_id == focused);
    match chrome.sidebar.machine_filter.as_deref() {
        Some(ALL_MACHINES) => true,
        Some(machine) => same_project && agent.machine_id == machine,
        None => same_project && agent.machine_id == ws.sidebar().local_machine,
    }
}

/// The tab set that shows `project`'s panes: the tab bar for the focused
/// project (or while no project is focused), else the project's own set.
fn tab_set<'a>(chrome: &'a Chrome, project: &str) -> Option<&'a TabSet> {
    match chrome.project_tabs.focused.as_deref() {
        Some(focused) if focused != project => chrome.project_tabs.sets.get(project),
        _ => Some(chrome.tabs()),
    }
}

/// Index of the tab showing the agent's pane in its project's set, with the
/// tab's title when the set has more than one tab.
fn tab_of<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    agent: &AgentEntry,
) -> (Option<usize>, Option<String>) {
    let Some(pane) = ws.pane_for_terminal(&agent.terminal_id) else {
        return (None, None);
    };
    let Some(set) = tab_set(chrome, &agent.project_id) else {
        return (None, None);
    };
    let index = set.tabs.iter().position(|tab| tab.slot_for(pane).is_some());
    let title = index
        .filter(|_| set.tabs.len() > 1)
        .map(|index| set.tabs[index].title.clone());
    (index, title)
}

/// The agents the filter admits, in the order the sort pref asks for:
/// `grouped` follows the tabs (rows no tab shows last, roster order within),
/// `priority` puts the most urgent first and the latest activity before the
/// rest (herdr `ordered_agent_pane_ids`).
fn visible_agents<'a, W: WorkspaceView>(
    ws: &'a W,
    chrome: &Chrome,
) -> Vec<(&'a AgentEntry, RowState, Option<String>)> {
    let mut agents: Vec<(&AgentEntry, RowState, Option<usize>, Option<String>)> = ws
        .sidebar()
        .agents
        .iter()
        .filter(|agent| admits(ws, chrome, agent))
        .map(|agent| {
            let (tab_index, tab_title) = tab_of(ws, chrome, agent);
            (agent, agent_state(ws, agent), tab_index, tab_title)
        })
        .collect();
    match chrome.prefs.agent_sort {
        AgentSort::Grouped => {
            agents.sort_by_key(|(_, _, tab_index, _)| tab_index.unwrap_or(usize::MAX));
        }
        AgentSort::Priority => agents.sort_by_key(|(agent, state, _, _)| {
            (
                Reverse(urgency(*state)),
                Reverse(agent.last_activity_at.clone()),
            )
        }),
    }
    agents
        .into_iter()
        .map(|(agent, state, _, tab_title)| (agent, state, tab_title))
        .collect()
}

/// One row per visible agent: the label and state on the first line, the
/// provider, model, task, tab, and remote machine tokens on the second.
pub fn agent_rows<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Vec<SidebarRow> {
    let focused = chrome.focused_pane();
    let local_machine = ws.sidebar().local_machine.as_str();
    visible_agents(ws, chrome)
        .into_iter()
        .map(|(agent, state, tab_title)| {
            let pane = ws.pane_for_terminal(&agent.terminal_id);
            let machine = (!agent.machine_id.is_empty() && agent.machine_id != local_machine)
                .then(|| short_terminal_id(&agent.machine_id).to_string());
            let tokens = [
                Some(agent.provider.clone()),
                agent.model.clone(),
                agent.task_ref.clone(),
                tab_title,
                machine,
            ]
            .into_iter()
            .flatten()
            .filter(|token| !token.is_empty())
            .collect();
            SidebarRow {
                id: agent.entry_id.clone(),
                label: agent_label(ws, agent),
                kind: RowKind::Agent,
                state,
                tokens,
                active: focused.is_some() && pane == focused,
                ..SidebarRow::default()
            }
        })
        .collect()
}

/// Entry ids the attention chords walk: the visible rows, blocked first.
pub fn attention_order<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Vec<String> {
    let agents = visible_agents(ws, chrome);
    let blocked = agents
        .iter()
        .filter(|(_, state, _)| *state == RowState::Attention);
    let rest = agents
        .iter()
        .filter(|(_, state, _)| *state != RowState::Attention);
    blocked
        .chain(rest)
        .map(|(agent, _, _)| agent.entry_id.clone())
        .collect()
}

/// Draw the section into `area` (the content rect, without the separator
/// column) and record its hits.
pub(super) fn render_agents<W: WorkspaceView>(
    frame: &mut Frame,
    area: Rect,
    ws: &W,
    chrome: &Chrome,
    hits: &mut SidebarHits,
) {
    let p = &chrome.palette;
    if area.width == 0 || area.height < AGENTS_HEADER_ROWS {
        return;
    }
    frame.render_widget(
        Paragraph::new(Span::styled(
            "─".repeat(usize::from(area.width)),
            Style::default().fg(p.surface_dim),
        )),
        Rect::new(area.x, area.y, area.width, 1),
    );
    let title_row = Rect::new(area.x, area.y + 1, area.width, 1);
    let title = " agents";
    frame.render_widget(
        Paragraph::new(Span::styled(
            title,
            Style::default().fg(p.overlay0).add_modifier(Modifier::BOLD),
        )),
        title_row,
    );
    let mut labels_x = area.right();
    let sort_label = chrome.prefs.agent_sort.label();
    if let Some(rect) = label_rect(title_row, &mut labels_x, sort_label, title) {
        frame.render_widget(
            Paragraph::new(Span::styled(
                sort_label,
                Style::default().fg(p.overlay0).add_modifier(Modifier::BOLD),
            )),
            rect,
        );
        hits.agent_sort = chrome.prefs.mouse_capture.then_some(rect);
    }
    if ws.sidebar().machines.len() > 1 {
        let filter = chrome.sidebar.machine_filter.as_deref();
        let label = machine_filter_label(filter);
        if let Some(rect) = label_rect(title_row, &mut labels_x, label, title) {
            let color = if filter.is_some() {
                p.accent
            } else {
                p.overlay0
            };
            frame.render_widget(
                Paragraph::new(Span::styled(label, Style::default().fg(color))),
                rect,
            );
            hits.machine_filter = chrome.prefs.mouse_capture.then_some(rect);
        }
    }

    let rows = agent_rows(ws, chrome);
    let heights: Vec<u16> = rows.iter().map(SidebarRow::height).collect();
    let viewport = agents_body_rect(area, false).height;
    let metrics = project_list_metrics(&heights, viewport, chrome.sidebar.agents_scroll);
    let body = agents_body_rect(area, should_show_scrollbar(metrics));
    render_rows(frame, body, &rows, metrics, chrome, hits);
    if should_show_scrollbar(metrics) {
        let track = scrollbar_track(area, body);
        render_scrollbar(frame, metrics, track, p.surface_dim, p.overlay0, "▕");
        hits.agents_scrollbar = Some(track);
    }
}

/// The cell run for a header label packed against the right edge, one
/// blank left of the label placed before it; `None` once it would overlap
/// the title.
fn label_rect(row: Rect, right: &mut u16, label: &str, title: &str) -> Option<Rect> {
    let width = u16::try_from(display_width(label)).ok()?;
    let x = right.checked_sub(width)?;
    if x < row.x + u16::try_from(display_width(title)).ok()? + 1 {
        return None;
    }
    *right = x.saturating_sub(1);
    Some(Rect::new(x, row.y, width, 1))
}

/// Rows from the scroll offset down while they fit whole; the focused
/// pane's row sits on `surface_dim` (herdr `render_agent_row`).
fn render_rows(
    frame: &mut Frame,
    body: Rect,
    rows: &[SidebarRow],
    metrics: gobby_terminal::layout::ScrollMetrics,
    chrome: &Chrome,
    hits: &mut SidebarHits,
) {
    let p = &chrome.palette;
    if body.width == 0 || body.height == 0 {
        return;
    }
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
        frame.render_widget(
            Paragraph::new(row_second_line(row, body.width, chrome)).style(row_style),
            Rect::new(body.x, y + 1, body.width, 1),
        );
        hits.agents
            .push((row.id.clone(), Rect::new(body.x, y, body.width, height)));
        y += height;
    }
}
