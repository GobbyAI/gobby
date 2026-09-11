// upstream: herdr v0.8.0 src/client/shell/agent_sidebar.rs
//! The sessions and agents sections: one two-line row per roster entry the
//! machine filter admits, the interactive sessions in one list and the
//! agent runs (the daemon's `run:` entries, agent-managed in the web UI) in
//! the other, in tab order or by urgency (`agent_sort`), each under a
//! three-row header; the agents header carries the sort label.
//!
//! herdr lists every workspace's agents and marks the view with a label in
//! the header; gclient's filter is the machine axis instead: the focused
//! project's local entries by default, one remote machine, or every entry
//! of every project and machine (`ALL_MACHINES`), chosen in the machines
//! section.

use std::cmp::Reverse;

use super::{render_header, render_section_rows, SidebarHits};
use crate::app::project_tabs::TabSet;
use crate::app::short_terminal_id;
use crate::app::sidebar_model::{agent_row_state, urgency, AgentEntry, SidebarModel};
use crate::ui::chrome::{Chrome, RowState, WorkspaceView};
use crate::ui::hit::SidebarSection;
use crate::ui::settings::AgentSort;
use crate::ui::sidebar_rows::{RowKind, SidebarRow};
use crate::ui::text::display_width;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::Span;
use ratatui::widgets::Paragraph;
use ratatui::Frame;

/// `SidebarState::machine_filter` value that admits every entry.
pub const ALL_MACHINES: &str = "all";

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

/// The filter after `current`: local, then every machine, then each remote
/// machine the model knows, then local again.
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

/// The row state: the pane's live state where one is attached, else the
/// roster's.
pub(super) fn agent_state<W: WorkspaceView>(ws: &W, agent: &AgentEntry) -> RowState {
    ws.pane_for_terminal(&agent.terminal_id)
        .map_or(agent.state, |pane| agent_row_state(agent, ws.pane(pane)))
}

/// Whether the entry is waiting on an attention prompt; an entry the model
/// no longer lists counts as blocked, so a stale row still opens respond.
pub fn agent_blocked<W: WorkspaceView>(ws: &W, entry_id: &str) -> bool {
    ws.sidebar()
        .agents
        .iter()
        .find(|agent| agent.entry_id == entry_id)
        .is_none_or(|agent| agent_state(ws, agent) == RowState::Attention)
}

/// The section that lists `entry_id`: agent runs under agents, everything
/// else (an entry the model no longer lists included) under sessions.
pub fn agent_section<W: WorkspaceView>(ws: &W, entry_id: &str) -> SidebarSection {
    let managed = ws
        .sidebar()
        .agents
        .iter()
        .find(|agent| agent.entry_id == entry_id)
        .is_some_and(|agent| agent.managed);
    if managed {
        SidebarSection::Agents
    } else {
        SidebarSection::Sessions
    }
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

/// The tab set the agent's project shows: the active set for the focused
/// project, the stored one for any other.
fn tab_set<'a>(chrome: &'a Chrome, project: &str) -> Option<&'a TabSet> {
    match chrome.project_tabs.focused.as_deref() {
        Some(focused) if focused != project => chrome.project_tabs.sets.get(project),
        _ => Some(chrome.tabs()),
    }
}

/// The tab that shows the agent's pane in its project's set, and its title
/// when the set has more than one tab (the row's tab token).
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

/// The entries the filter admits, of both sections, with their live state
/// and tab token, in the `agent_sort` order: by tab (entries without one
/// last) or by urgency then last activity.
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

/// The rows of `section`: the agent runs for the agents section, the
/// interactive sessions for any other. Each is the entry's label and state
/// over its provider, model, task ref, tab and remote machine tokens.
pub fn agent_rows<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    section: SidebarSection,
) -> Vec<SidebarRow> {
    let managed = section == SidebarSection::Agents;
    let focused = chrome.focused_pane();
    let local_machine = ws.sidebar().local_machine.as_str();
    visible_agents(ws, chrome)
        .into_iter()
        .filter(|(agent, _, _)| agent.managed == managed)
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

/// Entry ids of both sections in attention-walk order: the blocked ones
/// first, then the rest, each in the sections' row order.
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

/// Draw `section` (sessions or agents) into `area` (the content rect,
/// without the separator column) and record its hits.
pub(super) fn render_agents<W: WorkspaceView>(
    frame: &mut Frame,
    area: Rect,
    section: SidebarSection,
    ws: &W,
    chrome: &Chrome,
    hits: &mut SidebarHits,
) {
    let p = &chrome.palette;
    let Some(title_row) = render_header(frame, area, section, p) else {
        return;
    };
    if section == SidebarSection::Agents {
        let sort_label = chrome.prefs.agent_sort.label();
        if let Some(rect) = label_rect(title_row, area.right(), sort_label, section.title()) {
            frame.render_widget(
                Paragraph::new(Span::styled(
                    sort_label,
                    Style::default().fg(p.overlay0).add_modifier(Modifier::BOLD),
                )),
                rect,
            );
            hits.agent_sort = chrome.prefs.mouse_capture.then_some(rect);
        }
    }
    let rows = agent_rows(ws, chrome, section);
    let (drawn, lane) = render_section_rows(frame, area, section, &rows, chrome);
    hits.agents.extend(drawn);
    hits.scrollbars[section.index()] = lane;
}

/// Where a header label ending at `right` sits on the title row, unless it
/// would run into the title.
fn label_rect(row: Rect, right: u16, label: &str, title: &str) -> Option<Rect> {
    let width = u16::try_from(display_width(label)).ok()?;
    let x = right.checked_sub(width)?;
    if x < row.x + u16::try_from(display_width(title)).ok()? + 1 {
        return None;
    }
    Some(Rect::new(x, row.y, width, 1))
}
