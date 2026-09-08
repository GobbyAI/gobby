//! The sidebar's view model: projects with their worktrees and the agents
//! running in them, built from the daemon's typed rows, the attention roster,
//! and the open panes. `build` is pure so the chrome and the tests share one
//! join; the `Workspace` methods below own the cached inputs and the refetch
//! bookkeeping around it.

use std::collections::BTreeSet;
use std::path::PathBuf;
use std::time::Duration;

use serde_json::Value;
use tokio::time::Instant;

use crate::daemon::{Attention, Daemon, ProjectRow, RosterEntry, SidebarRows};
use crate::ui::chrome::RowState;

use super::{short_terminal_id, Pane, Workspace};

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
    pub branch: String,
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
    pub worktree_id: Option<String>,
    pub lifecycle_status: Option<String>,
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
            let name = session
                .and_then(|(_, session)| session.title.clone())
                .or_else(|| run.and_then(|(_, run)| run.agent_name.clone()))
                .or_else(|| entry.tmux_session_name.clone())
                .or_else(|| pane.map(|pane| pane.display_name().to_string()))
                .filter(|name| !name.is_empty())
                .unwrap_or_else(|| short_terminal_id(&terminal.terminal_id).to_string());
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
                provider: entry
                    .provider
                    .clone()
                    .or_else(|| run.and_then(|(_, run)| run.provider.clone()))
                    .or_else(|| session.and_then(|(_, session)| session.source.clone()))
                    .unwrap_or_default(),
                model: entry
                    .model
                    .clone()
                    .or_else(|| run.and_then(|(_, run)| run.model.clone())),
                task_ref: entry.task.as_ref().and_then(|task| task.reference.clone()),
                session_ref: session.and_then(|(_, session)| session.reference.clone()),
                worktree_id: run.and_then(|(_, run)| run.worktree_id.clone()),
                lifecycle_status: entry.lifecycle_status.clone(),
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
        RowState::Attention => 4,
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
    resolve_state(
        entry.attention.is_some(),
        entry.lifecycle_status.as_deref(),
        pane,
    )
}

/// `agent_state` for a built agent row and the pane the chrome found for it,
/// so `row_state` tracks pane flags that changed after the last rebuild.
pub fn agent_row_state(agent: &AgentEntry, pane: &Pane) -> RowState {
    resolve_state(
        agent.attention.is_some(),
        agent.lifecycle_status.as_deref(),
        Some(pane),
    )
}

fn resolve_state(blocked: bool, lifecycle_status: Option<&str>, pane: Option<&Pane>) -> RowState {
    if blocked {
        return RowState::Attention;
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
    pub(super) sessions: bool,
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
        let panes: Vec<&Pane> = self.panes.values().collect();
        self.sidebar = build(&SidebarInputs {
            local_machine: &self.local_machine,
            focused_project: self.project_id.as_deref(),
            rows: &self.sidebar_rows,
            roster: &self.attention.entries,
            panes: &panes,
            git_refreshed_at: self.git_refreshed_at,
        });
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
