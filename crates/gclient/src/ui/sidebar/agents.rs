// upstream: herdr v0.8.0 src/client/shell/agent_sidebar.rs
//! The agents section: one three-line row per roster entry the machine
//! filter and the scope admit — interactive sessions with the agent runs
//! they spawned nested under them, parentless runs at the top level, in tab
//! order or by urgency (`agent_sort`). View › Sidebar › Agents holds both
//! axes and the order.
//!
//! herdr lists every workspace's agents and marks the view with a label in
//! the header; gclient has two axes instead: the scope (the focused
//! project's rows, or every project's rows under a dim heading per project)
//! and the machine filter chosen in the machines section (the local
//! machine, one remote machine, or `ALL_MACHINES`).

use std::cmp::Reverse;

use super::{render_band, render_section_rows, SidebarHits};
use crate::app::project_tabs::TabSet;
use crate::app::sidebar_model::{agent_row_state, urgency, AgentEntry, SidebarModel};
use crate::ui::chrome::{Chrome, RowState, WorkspaceView};
use crate::ui::hit::SidebarSection;
use crate::ui::settings::AgentSort;
use crate::ui::sidebar_rows::{displayed_project_ids, project_label, RowKind, SidebarRow};
use ratatui::layout::Rect;
use ratatui::Frame;

/// `SidebarState::machine_filter` value that admits every machine.
pub const ALL_MACHINES: &str = "all";
/// Row id prefix of a bare terminal: `terminal:<terminal_id>`.
pub const TERMINAL_ROW: &str = "terminal:";
/// Row id prefix of a project heading of the all-projects list.
const GROUP_ROW: &str = "group:";

/// What the row calls the entry: `#ref: title` for a session, the ref
/// leading whether the daemon's title carried it (`gobby#12856: fix` reads
/// `#12856: fix`) or not; else the name with the tmux address that keeps
/// two same-named terminals apart, never a raw UUID.
pub fn agent_label(agent: &AgentEntry) -> String {
    match agent.session_ref.as_deref() {
        Some(reference) => match agent.name.find(reference) {
            Some(at) => agent.name[at..].to_string(),
            None => format!("{reference}: {}", agent.name),
        },
        None => agent.name.clone(),
    }
}

fn agent_title(agent: &AgentEntry) -> String {
    let Some(reference) = agent.session_ref.as_deref() else {
        return agent.name.clone();
    };
    let Some(at) = agent.name.find(reference) else {
        return agent.name.clone();
    };
    let title = agent.name[at + reference.len()..]
        .trim_start_matches(':')
        .trim();
    if title.is_empty() {
        agent.name.clone()
    } else {
        title.to_string()
    }
}

fn short_session_ref(reference: &str) -> &str {
    reference
        .rfind('#')
        .map_or(reference, |at| &reference[at..])
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
pub(crate) fn agent_state<W: WorkspaceView>(ws: &W, agent: &AgentEntry) -> RowState {
    let pane = ws
        .pane_for_terminal(&agent.terminal_id)
        .map(|pane| ws.pane(pane));
    agent_row_state(agent, pane)
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
        None => ws.sidebar().local_machine.is_empty() || machine_id == ws.sidebar().local_machine,
    }
}

/// Whether the scope and the machine filter admit `agent`.
fn admits<W: WorkspaceView>(ws: &W, chrome: &Chrome, agent: &AgentEntry) -> bool {
    let in_scope = chrome.sidebar.all_projects
        || ws
            .focused_project()
            .is_some_and(|focused| agent.project_id == focused);
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
fn tab_of<W: WorkspaceView>(ws: &W, chrome: &Chrome, agent: &AgentEntry) -> Option<usize> {
    let pane = ws.pane_for_terminal(&agent.terminal_id)?;
    let set = tab_set(chrome, &agent.project_id)?;
    set.tabs.iter().position(|tab| tab.slot_for(pane).is_some())
}

/// An admitted entry with its live state and tab.
struct Visible<'a> {
    agent: &'a AgentEntry,
    state: RowState,
    tab_index: Option<usize>,
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
        .map(|agent| Visible {
            agent,
            state: agent_state(ws, agent),
            tab_index: tab_of(ws, chrome, agent),
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

/// The rows: every admitted roster entry as a three-line definition, task
/// and model slug. Under `grouped` a run nests under the listed session that
/// spawned it; under `priority` the list is flat. Under the `all` scope
/// the rows sit under a heading per project, in the projects' order,
/// projects with nothing live omitted.
pub fn agent_rows<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Vec<SidebarRow> {
    let mut candidates: Vec<Candidate> = visible_agents(ws, chrome)
        .into_iter()
        .map(|visible| agent_candidate(ws, chrome, visible))
        .collect();
    if chrome.prefs.agent_sort == AgentSort::Priority {
        candidates.sort_by_key(|candidate| Reverse(urgency(candidate.row.state)));
    }
    if !chrome.sidebar.all_projects || !chrome.sidebar.all_sessions {
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
            label: project_label(ws, chrome, &project).unwrap_or_else(|| "Project".to_string()),
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

/// An agent's session ref as its row and pane corner print it: `#N`, led
/// by the project only where rows from every project mix.
pub(crate) fn agent_reference<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    agent: &AgentEntry,
) -> String {
    agent
        .session_ref
        .as_deref()
        .map_or_else(String::new, |reference| {
            let project = (chrome.sidebar.all_projects
                && (!chrome.sidebar.all_sessions
                    || chrome.prefs.agent_sort == AgentSort::Priority))
                .then(|| project_label(ws, chrome, &agent.project_id).unwrap_or_default());
            format!(
                "{}{reference}",
                project.as_deref().unwrap_or_default(),
                reference = short_session_ref(reference)
            )
        })
}

fn agent_candidate<W: WorkspaceView>(ws: &W, chrome: &Chrome, visible: Visible<'_>) -> Candidate {
    let Visible { agent, state, .. } = visible;
    let focused = chrome.focused_pane();
    let pane = ws.pane_for_terminal(&agent.terminal_id);
    Candidate {
        row: SidebarRow {
            id: agent.entry_id.clone(),
            definition: agent.definition_label(),
            reference: agent_reference(ws, chrome, agent),
            task: agent.task_ref.clone().zip(agent.task_title.clone()),
            model_slug: agent.model_slug(),
            label: agent_title(agent),
            kind: RowKind::Agent,
            state,
            active: focused.is_some() && pane == focused,
            ..SidebarRow::default()
        },
        project_id: agent.project_id.clone(),
        session_id: agent.session_id.clone(),
        parent_session_id: agent.parent_session_id.clone(),
    }
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
pub(super) fn render_agents(
    frame: &mut Frame,
    area: Rect,
    rows: &[SidebarRow],
    chrome: &Chrome,
    hits: &mut SidebarHits,
) {
    let section = SidebarSection::Agents;
    render_band(frame, area, section.title(), &chrome.palette);
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
