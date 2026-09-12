// upstream: herdr v0.8.0 src/client/shell/agent_sidebar.rs
//! The sessions section: one two-line row per roster entry the machine
//! filter and the scope admit — interactive sessions with the agent runs
//! they spawned nested under them, parentless runs at the top level — and
//! one per bare terminal (a pane no roster entry names), in tab order or by
//! urgency (`agent_sort`). The band carries the scope and sort controls.
//!
//! herdr lists every workspace's agents and marks the view with a label in
//! the header; gclient has two axes instead: the scope (`[project]`, the
//! focused project's rows, or `[all]`, every project's rows under a dim
//! heading per project) and the machine filter chosen in the machines
//! section (the local machine, one remote machine, or `ALL_MACHINES`).

use std::cmp::Reverse;

use super::{render_band, render_section_rows, BandStyle, SidebarHits};
use crate::app::project_tabs::TabSet;
use crate::app::short_terminal_id;
use crate::app::sidebar_model::{agent_row_state, pane_state, urgency, AgentEntry, SidebarModel};
use crate::ui::chrome::{Chrome, RowState, WorkspaceView};
use crate::ui::hit::SidebarSection;
use crate::ui::settings::AgentSort;
use crate::ui::sidebar_rows::{displayed_project_ids, project_label, RowKind, SidebarRow};
use ratatui::layout::Rect;
use ratatui::Frame;

/// `SidebarState::machine_filter` value that admits every machine.
pub const ALL_MACHINES: &str = "all";
/// The band's scope control while the focused project's rows are listed.
pub const PROJECT_SCOPE_LABEL: &str = "[project]";
/// The band's scope control while every project's rows are listed.
pub const ALL_SCOPE_LABEL: &str = "[all]";
/// Row id prefix of a bare terminal: `terminal:<terminal_id>`.
pub const TERMINAL_ROW: &str = "terminal:";
/// Row id prefix of a project heading of the all-projects list.
const GROUP_ROW: &str = "group:";

/// What the row calls the entry: `#ref: title` for a session, the ref
/// leading whether the daemon's title carried it (`gobby#12856: fix` reads
/// `#12856: fix`) or not; else the name with the tmux address that keeps
/// two same-named terminals apart, never a raw UUID.
pub fn agent_label<W: WorkspaceView>(ws: &W, agent: &AgentEntry) -> String {
    if let Some(reference) = agent.session_ref.as_deref() {
        return match agent.name.find(reference) {
            Some(at) => agent.name[at..].to_string(),
            None => format!("{reference}: {}", agent.name),
        };
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

/// What the sessions band's scope control reads.
pub fn sessions_scope_label(chrome: &Chrome) -> &'static str {
    if chrome.sidebar.all_sessions {
        ALL_SCOPE_LABEL
    } else {
        PROJECT_SCOPE_LABEL
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
/// A bare terminal has no prompt to answer.
pub fn agent_blocked<W: WorkspaceView>(ws: &W, entry_id: &str) -> bool {
    if entry_id.starts_with(TERMINAL_ROW) {
        return false;
    }
    ws.sidebar()
        .agents
        .iter()
        .find(|agent| agent.entry_id == entry_id)
        .is_none_or(|agent| agent_state(ws, agent) == RowState::Attention)
}

/// Whether the machine filter admits an entry on `machine_id`: the local
/// machine by default, every machine under `ALL_MACHINES`, else the one
/// machine chosen.
pub fn machine_admits<W: WorkspaceView>(ws: &W, chrome: &Chrome, machine_id: &str) -> bool {
    match chrome.sidebar.machine_filter.as_deref() {
        Some(ALL_MACHINES) => true,
        Some(machine) => machine_id == machine,
        None => machine_id == ws.sidebar().local_machine,
    }
}

/// Whether the scope and the machine filter admit `agent`.
fn admits<W: WorkspaceView>(ws: &W, chrome: &Chrome, agent: &AgentEntry) -> bool {
    let in_scope = chrome.sidebar.all_sessions
        || ws
            .focused_project()
            .is_none_or(|focused| agent.project_id == focused);
    in_scope && machine_admits(ws, chrome, &agent.machine_id)
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

/// An admitted entry with its live state and tab.
struct Visible<'a> {
    agent: &'a AgentEntry,
    state: RowState,
    tab_index: Option<usize>,
    tab_title: Option<String>,
}

/// The entries the scope and filter admit, with their live state and tab
/// token, in the `agent_sort` order: by tab (entries without one last) or
/// by urgency then last activity.
fn visible_agents<'a, W: WorkspaceView>(ws: &'a W, chrome: &Chrome) -> Vec<Visible<'a>> {
    let mut agents: Vec<Visible> = ws
        .sidebar()
        .agents
        .iter()
        .filter(|agent| admits(ws, chrome, agent))
        .map(|agent| {
            let (tab_index, tab_title) = tab_of(ws, chrome, agent);
            Visible {
                agent,
                state: agent_state(ws, agent),
                tab_index,
                tab_title,
            }
        })
        .collect();
    sort_visible(&mut agents, chrome.prefs.agent_sort);
    agents
}

fn sort_visible(agents: &mut [Visible<'_>], sort: AgentSort) {
    match sort {
        AgentSort::Grouped => {
            agents.sort_by_key(|visible| visible.tab_index.unwrap_or(usize::MAX));
        }
        AgentSort::Priority => agents.sort_by_key(|visible| {
            (
                Reverse(urgency(visible.state)),
                Reverse(visible.agent.last_activity_at.clone()),
            )
        }),
    }
}

/// A row before it is placed: the entry it came from and where it nests.
struct Candidate {
    row: SidebarRow,
    project_id: String,
    session_id: Option<String>,
    parent_session_id: Option<String>,
}

/// The rows: every admitted entry as a two-line row of its label and state
/// over its provider, model (with the reasoning effort), task ref or tab,
/// and remote machine tokens, then the bare terminals of the focused
/// project. Under `grouped` a run nests under the listed session that
/// spawned it; under `priority` the list is flat. Under the `all` scope
/// the rows sit under a heading per project, in the projects' order,
/// projects with nothing live omitted.
pub fn session_rows<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Vec<SidebarRow> {
    let mut candidates: Vec<Candidate> = visible_agents(ws, chrome)
        .into_iter()
        .map(|visible| agent_candidate(ws, chrome, visible))
        .collect();
    candidates.extend(bare_terminals(ws, chrome));
    if chrome.prefs.agent_sort == AgentSort::Priority {
        // Bare terminals carry no activity stamp and sort after the
        // entries of their urgency.
        candidates.sort_by_key(|candidate| Reverse(urgency(candidate.row.state)));
    }
    if !chrome.sidebar.all_sessions {
        return arrange(candidates, chrome.prefs.agent_sort);
    }
    let mut order = displayed_project_ids(ws, chrome);
    for candidate in &candidates {
        if !order.contains(&candidate.project_id) {
            order.push(candidate.project_id.clone());
        }
    }
    let mut rows = Vec::new();
    for project in order {
        let (own, rest): (Vec<Candidate>, Vec<Candidate>) = candidates
            .into_iter()
            .partition(|candidate| candidate.project_id == project);
        candidates = rest;
        if own.is_empty() {
            continue;
        }
        rows.push(SidebarRow {
            id: format!("{GROUP_ROW}{project}"),
            label: project_label(ws, chrome, &project).unwrap_or_else(|| project.clone()),
            kind: RowKind::Group,
            ..SidebarRow::default()
        });
        rows.extend(arrange(own, chrome.prefs.agent_sort));
    }
    rows
}

/// `candidates` in list order: under `grouped`, each parent followed by the
/// runs whose `parent_session_id` names its session, marked nested; under
/// `priority`, as they come.
fn arrange(candidates: Vec<Candidate>, sort: AgentSort) -> Vec<SidebarRow> {
    if sort == AgentSort::Priority {
        return candidates
            .into_iter()
            .map(|candidate| candidate.row)
            .collect();
    }
    let sessions: Vec<String> = candidates
        .iter()
        .filter_map(|candidate| candidate.session_id.clone())
        .collect();
    let nests = |candidate: &Candidate| -> bool {
        candidate
            .parent_session_id
            .as_ref()
            .is_some_and(|parent| sessions.contains(parent))
    };
    let (children, parents): (Vec<Candidate>, Vec<Candidate>) =
        candidates.into_iter().partition(nests);
    let mut rows = Vec::new();
    let mut children: Vec<Option<Candidate>> = children.into_iter().map(Some).collect();
    for parent in parents {
        let session = parent.session_id.clone();
        rows.push(parent.row);
        push_children(&session, &mut children, &mut rows);
    }
    rows
}

/// Move the `children` whose parent is `session` into `rows`, each followed
/// by its own children in turn (a run's run nests too); every candidate
/// is taken once, so a cycle ends.
fn push_children(
    session: &Option<String>,
    children: &mut [Option<Candidate>],
    rows: &mut Vec<SidebarRow>,
) {
    let own: Vec<Candidate> = children
        .iter_mut()
        .filter(|child| {
            child
                .as_ref()
                .is_some_and(|child| child.parent_session_id == *session)
        })
        .filter_map(Option::take)
        .collect();
    let count = own.len();
    for (index, mut child) in own.into_iter().enumerate() {
        child.row.nested = true;
        child.row.last_child = index + 1 == count;
        let session = child.session_id.clone();
        rows.push(child.row);
        push_children(&session, children, rows);
    }
}

fn agent_candidate<W: WorkspaceView>(ws: &W, chrome: &Chrome, visible: Visible<'_>) -> Candidate {
    let Visible {
        agent,
        state,
        tab_title,
        ..
    } = visible;
    let focused = chrome.focused_pane();
    let local_machine = ws.sidebar().local_machine.as_str();
    let pane = ws.pane_for_terminal(&agent.terminal_id);
    let machine = (!agent.machine_id.is_empty() && agent.machine_id != local_machine)
        .then(|| short_terminal_id(&agent.machine_id).to_string());
    let model = agent
        .model
        .clone()
        .map(|model| match agent.effort.as_deref() {
            Some(effort) => format!("{model}-{effort}"),
            None => model,
        });
    let tokens = [
        Some(agent.provider.clone()),
        model,
        agent.task_ref.clone().or(tab_title),
        machine,
    ]
    .into_iter()
    .flatten()
    .filter(|token| !token.is_empty())
    .collect();
    Candidate {
        row: SidebarRow {
            id: agent.entry_id.clone(),
            label: agent_label(ws, agent),
            kind: RowKind::Agent,
            state,
            tokens,
            active: focused.is_some() && pane == focused,
            ..SidebarRow::default()
        },
        project_id: agent.project_id.clone(),
        session_id: agent.session_id.clone(),
        parent_session_id: agent.parent_session_id.clone(),
    }
}

/// The focused project's panes no roster entry names, when the machine
/// filter admits this machine: the pane's name over its title and address
/// where they add something, and its backend.
fn bare_terminals<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Vec<Candidate> {
    let model = ws.sidebar();
    if !machine_admits(ws, chrome, &model.local_machine) {
        return Vec::new();
    }
    let focused = chrome.focused_pane();
    let project = ws.focused_project().unwrap_or_default().to_string();
    ws.roster_terminal_ids()
        .into_iter()
        .filter(|terminal_id| {
            !model
                .agents
                .iter()
                .any(|agent| agent.terminal_id == *terminal_id)
        })
        .filter_map(|terminal_id| {
            let pane_id = ws.pane_for_terminal(&terminal_id)?;
            let pane = ws.pane(pane_id);
            let name = pane.display_name().to_string();
            let tokens = [
                Some(pane.title.clone()).filter(|title| *title != name),
                pane.address.clone().filter(|address| *address != name),
                Some(pane.backend.clone()),
            ]
            .into_iter()
            .flatten()
            .filter(|token| !token.is_empty())
            .collect();
            Some(Candidate {
                row: SidebarRow {
                    id: format!("{TERMINAL_ROW}{terminal_id}"),
                    label: name,
                    kind: RowKind::Agent,
                    state: pane_state(pane),
                    tokens,
                    active: focused == Some(pane_id),
                    ..SidebarRow::default()
                },
                project_id: project.clone(),
                session_id: None,
                parent_session_id: None,
            })
        })
        .collect()
}

/// Entry ids in attention-walk order: the blocked ones first, then the
/// rest, each in the list's row order.
pub fn attention_order<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Vec<String> {
    let agents = visible_agents(ws, chrome);
    let blocked = agents
        .iter()
        .filter(|visible| visible.state == RowState::Attention);
    let rest = agents
        .iter()
        .filter(|visible| visible.state != RowState::Attention);
    blocked
        .chain(rest)
        .map(|visible| visible.agent.entry_id.clone())
        .collect()
}

/// Draw the section into `area` (the content rect, without the separator
/// column) and record its hits.
pub(super) fn render_sessions(
    frame: &mut Frame,
    area: Rect,
    rows: &[SidebarRow],
    chrome: &Chrome,
    hits: &mut SidebarHits,
) {
    let section = SidebarSection::Sessions;
    let sort = format!("[{}]", chrome.prefs.agent_sort.label());
    let (_, controls) = render_band(
        frame,
        area,
        section.title(),
        &[sessions_scope_label(chrome), &sort],
        BandStyle::section(&chrome.palette),
    );
    hits.sessions_scope = controls.first().copied();
    hits.agent_sort = controls.get(1).copied();
    render_section_rows(frame, area, section, rows, chrome, hits);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn candidate(id: &str, session: Option<&str>, parent: Option<&str>) -> Candidate {
        Candidate {
            row: SidebarRow {
                id: id.to_string(),
                kind: RowKind::Agent,
                ..SidebarRow::default()
            },
            project_id: String::new(),
            session_id: session.map(str::to_owned),
            parent_session_id: parent.map(str::to_owned),
        }
    }

    #[test]
    fn grouped_rows_nest_a_run_under_a_nested_run() {
        // A session, a run it spawned, a run that run spawned, and a run
        // whose parent is not listed: the chain stays together under the
        // session and the orphan keeps the top level.
        let rows = arrange(
            vec![
                candidate("session:a", Some("a"), None),
                candidate("run:c", Some("c"), Some("b")),
                candidate("run:d", None, Some("zz")),
                candidate("run:b", Some("b"), Some("a")),
            ],
            AgentSort::Grouped,
        );
        let ids: Vec<&str> = rows.iter().map(|row| row.id.as_str()).collect();
        assert_eq!(ids, ["session:a", "run:b", "run:c", "run:d"]);
        assert!(rows[1].nested && rows[1].last_child);
        assert!(rows[2].nested && rows[2].last_child);
        assert!(!rows[3].nested);
        // Priority keeps the flattened order.
        let rows = arrange(
            vec![
                candidate("run:c", Some("c"), Some("b")),
                candidate("session:a", Some("a"), None),
            ],
            AgentSort::Priority,
        );
        assert_eq!(rows[0].id, "run:c");
        assert!(!rows[0].nested);
    }
}
