//! The sidebar's view model: projects with their worktrees and the agents
//! running in them, built from the daemon's typed rows, the attention roster,
//! and the open panes. `build` is pure so the chrome and the tests share one
//! join; the `Workspace` methods below own the cached inputs and the refetch
//! bookkeeping around it.

use std::collections::{BTreeSet, HashMap};
use std::path::PathBuf;
use std::time::Duration;

use serde_json::Value;
use tokio::time::Instant;

use crate::daemon::{Attention, Daemon, ProjectRow, RosterEntry, SidebarRows};
use crate::ui::chrome::RowState;

use super::{Pane, Workspace, UNNAMED_PANE};

/// How long a project's source status stays fresh while the sidebar is open.
pub const GIT_REFRESH_INTERVAL: Duration = Duration::from_secs(10);

/// Projects the daemon keeps for bookkeeping; none of them is a workspace.
const HIDDEN_PROJECT_NAMES: [&str; 3] = ["_orphaned", "_migrated", "_global"];
/// `display_name` of the catch-all project, which sorts after every real one.
const PERSONAL_PROJECT: &str = "Personal";

#[derive(Debug, Clone, PartialEq)]
pub struct SidebarModel {
    pub local_machine: String,
    /// Every machine an agent runs on plus this one, sorted and unique.
    pub machines: Vec<String>,
    pub projects: Vec<ProjectEntry>,
    pub agents: Vec<AgentEntry>,
    pub git_refreshed_at: Instant,
}

impl Default for SidebarModel {
    fn default() -> Self {
        Self {
            local_machine: String::new(),
            machines: Vec::new(),
            projects: Vec::new(),
            agents: Vec::new(),
            git_refreshed_at: Instant::now(),
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct ProjectEntry {
    pub project_id: String,
    pub name: String,
    pub root_path: Option<PathBuf>,
    pub branch: Option<String>,
    pub ahead: Option<u32>,
    pub behind: Option<u32>,
    pub worktrees: Vec<WorktreeEntry>,
    pub state: RowState,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct WorktreeEntry {
    pub worktree_id: String,
    pub branch: Option<String>,
    pub path: PathBuf,
    pub task_ref: Option<String>,
    pub role: String,
    pub state: RowState,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct AgentEntry {
    pub entry_id: String,
    pub project_id: String,
    pub machine_id: String,
    pub terminal_id: String,
    pub backend: String,
    pub name: String,
    pub provider: String,
    pub model: Option<String>,
    pub task_ref: Option<String>,
    /// The joined session's `ref`, `#12217`, when the entry is session keyed.
    pub session_ref: Option<String>,
    /// The session the entry runs as: the roster's, else a run's child
    /// session; runs nest under the entry whose session they name.
    pub session_id: Option<String>,
    /// The session that spawned the entry, when the daemon knows one.
    pub parent_session_id: Option<String>,
    /// The resolved reasoning effort, shown after the model.
    pub effort: Option<String>,
    /// An agent run (the roster's `run:` entries), listed under agents;
    /// every other entry is an interactive session.
    pub managed: bool,
    pub worktree_id: Option<String>,
    pub lifecycle_status: Option<String>,
    /// The terminal row's daemon state; `orphaned` rows have lost their host.
    pub terminal_state: Option<String>,
    pub state: RowState,
    pub attention: Option<Attention>,
    pub last_activity_at: Option<String>,
}

/// Everything `build` reads, borrowed from the workspace or a test.
pub struct SidebarInputs<'a> {
    pub local_machine: &'a str,
    pub focused_project: Option<&'a str>,
    pub rows: &'a SidebarRows,
    pub roster: &'a [RosterEntry],
    pub panes: &'a [&'a Pane],
    pub git_refreshed_at: Instant,
}

pub fn build(inputs: &SidebarInputs) -> SidebarModel {
    let agents = build_agents(inputs);
    let mut projects: Vec<ProjectEntry> = inputs
        .rows
        .projects
        .iter()
        .filter(|row| !HIDDEN_PROJECT_NAMES.contains(&row.name.as_str()))
        .map(|row| project_entry(row, inputs, &agents))
        .collect();
    // Stable, so daemon order holds among the real projects.
    projects.sort_by_key(|project| project.name == PERSONAL_PROJECT);
    let mut machines: BTreeSet<String> = agents
        .iter()
        .map(|agent| agent.machine_id.clone())
        .chain(std::iter::once(inputs.local_machine.to_string()))
        .filter(|machine| !machine.is_empty())
        .collect();
    let machines = std::mem::take(&mut machines).into_iter().collect();
    SidebarModel {
        local_machine: inputs.local_machine.to_string(),
        machines,
        projects,
        agents,
        git_refreshed_at: inputs.git_refreshed_at,
    }
}

/// Roster entries with a terminal, joined to the focused project's sessions
/// and runs. Terminal rows, sessions, and runs are all fetched for the
/// focused project, so an entry that joins neither is placed there too.
fn build_agents(inputs: &SidebarInputs) -> Vec<AgentEntry> {
    let rows = inputs.rows;
    inputs
        .roster
        .iter()
        .filter_map(|entry| {
            let terminal = entry.terminal.as_ref()?;
            let session = entry.session_id.as_deref().and_then(|id| {
                rows.sessions.iter().find_map(|(project, sessions)| {
                    let session = sessions.iter().find(|session| session.id == id)?;
                    Some((project.as_str(), session))
                })
            });
            let run = entry.run_id.as_deref().and_then(|id| {
                rows.runs.iter().find_map(|(project, runs)| {
                    let run = runs.iter().find(|run| run.run_id == id)?;
                    Some((project.as_str(), run))
                })
            });
            let pane = inputs
                .panes
                .iter()
                .copied()
                .find(|pane| pane.terminal_id == terminal.terminal_id);
            let project_id = session
                .map(|(project, _)| project)
                .or(run.map(|(project, _)| project))
                .or(inputs.focused_project)
                .unwrap_or_default();
            let provider = entry
                .provider
                .clone()
                .or_else(|| run.and_then(|(_, run)| run.provider.clone()))
                .or_else(|| session.and_then(|(_, session)| session.source.clone()))
                .filter(|provider| !provider.is_empty());
            // Each rung is filtered on its own: an empty session title means
            // "unnamed", not "stop looking", so the run and tmux names below it
            // still get their turn. The pane's own ladder ends the chain for a
            // row that has one, and the two rungs after it cover a roster entry
            // with no pane open — neither can be an id.
            let name = session
                .and_then(|(_, session)| session.title.clone())
                .filter(|name| !name.is_empty())
                .or_else(|| {
                    run.and_then(|(_, run)| run.agent_name.clone())
                        .filter(|name| !name.is_empty())
                })
                .or_else(|| {
                    entry
                        .tmux_session_name
                        .clone()
                        .filter(|name| !name.is_empty())
                })
                .or_else(|| pane.map(|pane| pane.display_name().to_string()))
                .or_else(|| provider.clone())
                .unwrap_or_else(|| UNNAMED_PANE.to_string());
            let machine_id = session
                .and_then(|(_, session)| session.machine_id.clone())
                .or_else(|| run.and_then(|(_, run)| run.machine_id.clone()))
                .filter(|machine| !machine.is_empty())
                .unwrap_or_else(|| inputs.local_machine.to_string());
            Some(AgentEntry {
                entry_id: entry.entry_id.clone(),
                project_id: project_id.to_string(),
                machine_id,
                terminal_id: terminal.terminal_id.clone(),
                backend: terminal.backend.clone(),
                name,
                provider: provider.unwrap_or_default(),
                model: entry
                    .model
                    .clone()
                    .or_else(|| run.and_then(|(_, run)| run.model.clone())),
                task_ref: entry.task.as_ref().and_then(|task| task.reference.clone()),
                session_ref: session.and_then(|(_, session)| session.reference.clone()),
                session_id: entry
                    .session_id
                    .clone()
                    .or_else(|| run.and_then(|(_, run)| run.child_session_id.clone())),
                parent_session_id: run
                    .and_then(|(_, run)| run.parent_session_id.clone())
                    .or_else(|| session.and_then(|(_, session)| session.parent_session_id.clone())),
                effort: run
                    .and_then(|(_, run)| run.reasoning_effort().map(str::to_owned))
                    .or_else(|| {
                        session
                            .and_then(|(_, session)| session.reasoning_effort().map(str::to_owned))
                    }),
                managed: entry.run_id.is_some() || entry.entry_id.starts_with("run:"),
                worktree_id: run.and_then(|(_, run)| run.worktree_id.clone()),
                lifecycle_status: entry.lifecycle_status.clone(),
                terminal_state: terminal.state.clone(),
                state: agent_state(entry, pane),
                attention: entry.attention.clone(),
                last_activity_at: entry.last_activity_at.clone(),
            })
        })
        .collect()
}

fn project_entry(row: &ProjectRow, inputs: &SidebarInputs, agents: &[AgentEntry]) -> ProjectEntry {
    let status = inputs.rows.statuses.get(&row.id);
    let own: Vec<&AgentEntry> = agents
        .iter()
        .filter(|agent| agent.project_id == row.id)
        .collect();
    let worktrees = inputs
        .rows
        .worktrees
        .iter()
        .filter(|worktree| worktree.project_id == row.id)
        .map(|worktree| {
            let bound: Vec<&AgentEntry> = own
                .iter()
                .copied()
                .filter(|agent| agent.worktree_id.as_deref() == Some(worktree.id.as_str()))
                .collect();
            WorktreeEntry {
                worktree_id: worktree.id.clone(),
                branch: worktree.branch_name.clone(),
                path: PathBuf::from(&worktree.worktree_path),
                task_ref: bound
                    .iter()
                    .find_map(|agent| agent.task_ref.clone())
                    .or_else(|| worktree.task_id.clone()),
                role: worktree.workspace_role.clone(),
                state: most_urgent(bound.iter().map(|agent| agent.state)),
            }
        })
        .collect();
    ProjectEntry {
        project_id: row.id.clone(),
        name: if row.display_name.is_empty() {
            row.name.clone()
        } else {
            row.display_name.clone()
        },
        root_path: row
            .checkout
            .as_ref()
            .map(|checkout| PathBuf::from(&checkout.root_path)),
        branch: status.and_then(|status| status.current_branch.clone()),
        ahead: status.and_then(|status| status.ahead),
        behind: status.and_then(|status| status.behind),
        worktrees,
        state: most_urgent(own.iter().map(|agent| agent.state)),
    }
}

/// herdr `status_priority`: blocked over unseen over working over idle.
pub fn urgency(state: RowState) -> u8 {
    match state {
        RowState::Attention => 5,
        RowState::Orphaned => 4,
        RowState::Unseen => 3,
        RowState::Working => 2,
        RowState::Idle => 1,
        RowState::Unknown => 0,
    }
}

/// herdr's collapsed parent: the most urgent of its children's states.
fn most_urgent(states: impl Iterator<Item = RowState>) -> RowState {
    states
        .max_by_key(|state| urgency(*state))
        .unwrap_or(RowState::Idle)
}

/// herdr's `AgentState` for one roster entry, given the pane that shows it.
pub fn agent_state(entry: &RosterEntry, pane: Option<&Pane>) -> RowState {
    let terminal_state = entry
        .terminal
        .as_ref()
        .and_then(|terminal| terminal.state.as_deref());
    resolve_state(
        entry.attention.is_some(),
        is_orphaned(terminal_state),
        entry.lifecycle_status.as_deref(),
        pane,
    )
}

/// `orphaned` is the daemon's state for a row whose host is gone.
pub fn is_orphaned(terminal_state: Option<&str>) -> bool {
    terminal_state == Some("orphaned")
}

/// `agent_state` for a built agent row and the pane the chrome found for it,
/// so `row_state` tracks pane flags that changed after the last rebuild.
pub fn agent_row_state(agent: &AgentEntry, pane: &Pane) -> RowState {
    resolve_state(
        agent.attention.is_some(),
        is_orphaned(agent.terminal_state.as_deref()),
        agent.lifecycle_status.as_deref(),
        Some(pane),
    )
}

fn resolve_state(
    blocked: bool,
    orphaned: bool,
    lifecycle_status: Option<&str>,
    pane: Option<&Pane>,
) -> RowState {
    if blocked {
        return RowState::Attention;
    }
    if orphaned {
        return RowState::Orphaned;
    }
    let Some(pane) = pane else {
        return RowState::Idle;
    };
    let running = lifecycle_status.is_some_and(|status| {
        matches!(status, "running" | "active") || status.starts_with("awaiting_")
    });
    if pane.new_output && pane.live && running {
        RowState::Working
    } else if pane.new_output {
        RowState::Unseen
    } else {
        RowState::Idle
    }
}

/// The pane-only half of `agent_state`, for a terminal no roster entry names.
pub fn pane_state(pane: &Pane) -> RowState {
    if pane.new_output && pane.live {
        RowState::Working
    } else if pane.new_output {
        RowState::Unseen
    } else {
        RowState::Idle
    }
}

/// Refetches queued by live events, flushed once per drain or render tick.
#[derive(Debug, Default)]
pub(super) struct PendingSidebar {
    pub(super) projects: bool,
    pub(super) project_rows: BTreeSet<String>,
    /// Every tracked project's sessions and runs, for the events that name
    /// no project and the paths that change which projects are tracked.
    pub(super) sessions: bool,
    /// The projects named session events asked for. A session event carries
    /// its `project_id`, so one session changing costs one project's
    /// refetch rather than a sweep over every checkout.
    pub(super) session_rows: BTreeSet<String>,
    /// The attention roster: a session ending is the only signal that an
    /// agent run left it, since attention events never remove an entry.
    pub(super) roster: bool,
}

/// The refetch each sidebar row set came from. A refetch runs beside the
/// loop, so one started earlier can land after a later one for the same
/// rows; the rows keep the newest refetch and the late one is dropped.
#[derive(Debug, Default)]
pub(super) struct SidebarStamps {
    last: u64,
    pub(super) projects: u64,
    pub(super) project_rows: HashMap<String, u64>,
    pub(super) sessions: HashMap<String, u64>,
    pub(super) roster: u64,
}

impl SidebarStamps {
    /// The sequence of the refetch starting now.
    pub(super) fn next(&mut self) -> u64 {
        self.last += 1;
        self.last
    }

    /// Every row set counts as produced by `seq`.
    pub(super) fn stamp_all(&mut self, seq: u64) {
        self.projects = seq;
        self.project_rows
            .values_mut()
            .for_each(|stamp| *stamp = seq);
        self.sessions.values_mut().for_each(|stamp| *stamp = seq);
    }

    /// Whether rows from `seq` are newer than the ones `stamp` records;
    /// when they are, `stamp` moves to `seq`.
    pub(super) fn accept(stamp: &mut u64, seq: u64) -> bool {
        if seq > *stamp {
            *stamp = seq;
            true
        } else {
            false
        }
    }
}

impl<D: Daemon> Workspace<D> {
    pub fn sidebar(&self) -> &SidebarModel {
        &self.sidebar
    }

    pub fn roster_terminal_ids(&self) -> Vec<String> {
        self.roster_ids.clone()
    }

    pub fn attention_entry_ids(&self) -> Vec<String> {
        self.attention
            .entries
            .iter()
            .map(|entry| entry.entry_id.clone())
            .collect()
    }

    pub fn set_local_machine(&mut self, machine_id: impl Into<String>) {
        self.local_machine = machine_id.into();
        self.rebuild_sidebar();
    }

    pub(super) fn rebuild_sidebar(&mut self) {
        // Rung 2 of the label ladder is both produced and consumed by the
        // build: `build_agents` names an agent row from its pane's
        // `display_name`, which asks the pane for its provider. Syncing after
        // one build would answer that read with the previous build's provider,
        // so a terminal whose session has just been bound would read `shell`
        // until the next roster event. Build, sync, and build again only when
        // the sync moved something; in the steady state nothing moves.
        let model = self.build_sidebar();
        self.sidebar = if self.sync_pane_providers(&model) {
            self.build_sidebar()
        } else {
            model
        };
    }

    fn build_sidebar(&self) -> SidebarModel {
        let panes: Vec<&Pane> = self.panes.values().collect();
        build(&SidebarInputs {
            local_machine: &self.local_machine,
            focused_project: self.project_id.as_deref(),
            rows: &self.sidebar_rows,
            roster: &self.attention.entries,
            panes: &panes,
            git_refreshed_at: self.git_refreshed_at,
        })
    }

    /// Copy each agent's resolved provider onto the pane holding its terminal.
    ///
    /// Rung 2 of the label ladder is the provider of the bound session, which
    /// lives on the roster rather than the terminal row, and `display_name`
    /// takes only `&self`. The sidebar already joins roster entry, agent run
    /// and session to resolve it, so the pane borrows that answer instead of
    /// redoing the joins, and every rebuild refreshes it.
    fn sync_pane_providers(&mut self, model: &SidebarModel) -> bool {
        let mut resolved: HashMap<&str, &str> = HashMap::new();
        for agent in &model.agents {
            if !agent.provider.is_empty() {
                resolved.insert(agent.terminal_id.as_str(), agent.provider.as_str());
            }
        }
        let mut moved = false;
        for pane in self.panes.values_mut() {
            let provider = resolved
                .get(pane.terminal_id.as_str())
                .map(|provider| (*provider).to_string());
            if pane.provider != provider {
                pane.provider = provider;
                moved = true;
            }
        }
        moved
    }

    /// Upsert the roster entry an `attention_changed` event names. The event
    /// carries the entry under `metadata` when the daemon built one, else its
    /// flat fields double as the entry; only an explicit `state` moves the
    /// blocked flag, so a metadata-only event keeps what the roster said.
    pub(super) fn note_attention_event(&mut self, payload: &Value) {
        let source = payload
            .get("metadata")
            .filter(|metadata| metadata.get("entry_id").is_some())
            .unwrap_or(payload);
        let Ok(parsed) = serde_json::from_value::<RosterEntry>(source.clone()) else {
            return;
        };
        match self
            .attention
            .entries
            .iter_mut()
            .find(|known| known.entry_id == parsed.entry_id)
        {
            Some(known) => {
                if let Some(state) = source.get("state").and_then(Value::as_str) {
                    known.attention = (state == "blocked")
                        .then(|| serde_json::from_value::<Attention>(source.clone()).ok())
                        .flatten();
                }
            }
            None => self.attention.entries.push(parsed),
        }
    }
}
