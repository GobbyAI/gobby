//! The sidebar's view model: projects with their worktrees and the agents
//! running in them, built from the daemon's typed rows, the attention roster,
//! and the open panes. `build` is pure so the chrome and the tests share one
//! join; the `Workspace` methods below own the cached inputs and the refetch
//! bookkeeping around it.

use std::cmp::Reverse;
use std::collections::{BTreeSet, HashMap};
use std::path::PathBuf;
use std::time::Duration;

use serde_json::Value;
use tokio::time::Instant;

use crate::daemon::{Attention, Daemon, ProjectRow, RosterEntry, SidebarRows};
use crate::ui::chrome::RowState;

use super::{short_terminal_id, Backend, Pane, Workspace};

/// How long a project's source status stays fresh while the sidebar is open.
pub const GIT_REFRESH_INTERVAL: Duration = Duration::from_secs(10);

/// How long the attention roster may go without a refetch. Events keep it
/// current; this bounds how long a missed event can leave a glyph stale.
pub const ROSTER_REFRESH_INTERVAL: Duration = Duration::from_secs(15);

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

impl SidebarModel {
    /// Agents contributing to worktree, project, machine and status roll-ups.
    pub fn rollup_agents(&self) -> impl Iterator<Item = &AgentEntry> {
        self.agents.iter().filter(|agent| {
            !self.projects.iter().any(|project| {
                project.project_id == agent.project_id
                    && superseded_in_worktrees(agent, &self.agents, &project.worktrees)
            })
        })
    }
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
    /// Agents under this project that need the operator.
    pub needs_you: usize,
    /// Agents under this project with no live process.
    pub no_live: usize,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct WorktreeEntry {
    pub worktree_id: String,
    pub branch: Option<String>,
    pub path: PathBuf,
    pub task_ref: Option<String>,
    /// The task the worktree was made for; an unbound agent on it is bound here.
    pub task_id: Option<String>,
    pub role: String,
    /// The rolled-up state of the agents bound to it; blank with no agent,
    /// since a worktree has no state of its own.
    pub state: RowState,
}

impl WorktreeEntry {
    pub fn display_name(&self) -> String {
        if let Some(branch) = self
            .branch
            .as_deref()
            .filter(|branch| !branch.trim().is_empty())
        {
            return branch.to_owned();
        }
        let name = self
            .path
            .file_name()
            .map(|name| name.to_string_lossy())
            .unwrap_or_else(|| self.worktree_id.as_str().into());
        format!("{name} (detached)")
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct AgentEntry {
    pub entry_id: String,
    pub project_id: String,
    pub machine_id: String,
    pub terminal_id: String,
    pub backend: Backend,
    pub name: String,
    /// The run's agent definition; no rung of `name`.
    pub agent_definition_name: Option<String>,
    pub provider: String,
    pub model: Option<String>,
    /// The model's provider-printed name, resolved daemon-side; the row
    /// shows `model` when the daemon knew no better.
    pub model_display_name: Option<String>,
    pub task_ref: Option<String>,
    /// The roster task's daemon id, which an epic's integration tree matches.
    pub task_id: Option<String>,
    /// The roster task's title: a run's task, or the open task a session holds.
    pub task_title: Option<String>,
    /// True when this seat's session holds the claim on its task.
    pub task_claim_held: bool,
    /// The session's one-line status, drawn on line three while no task is held.
    pub status_line: Option<String>,
    /// The joined session's `ref`, `#12217`, when the entry is session keyed.
    pub session_ref: Option<String>,
    /// The session the entry runs as: the roster's, else a run's child
    /// session; runs nest under the entry whose session they name.
    pub session_id: Option<String>,
    /// The session that spawned the entry, when the daemon knows one.
    pub parent_session_id: Option<String>,
    /// The resolved reasoning effort, shown after the model.
    pub effort: Option<String>,
    pub context_percent: Option<u8>,
    pub tokens_used: Option<u64>,
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
    /// Whether an OS sandbox wraps the agent's process.
    pub sandbox: SandboxState,
}

/// Whether Gobby's SRT launch sandbox wraps a pane's process.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum SandboxState {
    Sandboxed,
    /// Every pane Gobby did not launch under SRT: hand-opened seats, bare
    /// shells, and panes with no agent row.
    #[default]
    Unrestricted,
}

impl SandboxState {
    /// Locked only on a Gobby launch record: the run's SRT record says it was
    /// enforced, or a managed session's launch contract enabled it. A direct
    /// session's own claim never locks the pane.
    pub fn resolve(session: Option<bool>, run: Option<bool>, managed: bool) -> Self {
        if run == Some(true) || (managed && session == Some(true)) {
            Self::Sandboxed
        } else {
            Self::Unrestricted
        }
    }

    /// The word the state stands for, for help text and the text glyphs.
    pub fn label(self) -> &'static str {
        match self {
            Self::Sandboxed => "sandboxed",
            Self::Unrestricted => "unrestricted",
        }
    }
}

impl AgentEntry {
    /// The agent definition name, else the provider label a seat with no definition shows.
    pub fn definition_label(&self) -> String {
        self.agent_definition_name
            .clone()
            .unwrap_or_else(|| provider_label(&self.provider).to_string())
    }

    /// `claude-fable-5.1-xhigh`: the display name (else the raw model) lowercased,
    /// each whitespace run one `-`, with the effort appended. When the display
    /// name drops the family (`Fable 5.1`), a hyphenated raw id's first token
    /// leads; bare aliases add no prefix. An absent segment leaves no dangling
    /// `-`: a run spawned with an effort before its model is known reads `xhigh`.
    pub fn model_slug(&self) -> String {
        let model = self
            .model_display_name
            .as_deref()
            .or(self.model.as_deref())
            .unwrap_or_default()
            .to_lowercase();
        let mut slug = model.split_whitespace().collect::<Vec<_>>().join("-");
        let family = self
            .model
            .as_deref()
            .and_then(|raw| raw.split_once('-').map(|(family, _)| family))
            .map(str::to_lowercase)
            .filter(|family| !family.is_empty());
        if let Some(family) = family {
            if !slug.is_empty() && slug.split('-').next() != Some(family.as_str()) {
                slug = format!("{family}-{slug}");
            }
        }
        if let Some(effort) = &self.effort {
            if !slug.is_empty() {
                slug.push('-');
            }
            slug.push_str(effort);
        }
        slug
    }
}

/// The provider labels a seat with no agent definition shows, kept in step with the daemon.
const PROVIDER_LABELS: [(&str, &str); 9] = [
    ("agy", "Antigravity"),
    ("claude", "Claude"),
    ("claude_code", "Claude Code"),
    ("codex", "Codex"),
    ("droid", "Droid"),
    ("grok", "Grok"),
    ("pipeline", "Pipeline"),
    ("system", "System"),
    ("unknown", "Unknown"),
];

/// A blank provider is `Unknown`; an unlisted one shows as written.
pub fn provider_label(provider: &str) -> &str {
    let provider = provider.trim();
    if provider.is_empty() {
        return "Unknown";
    }
    PROVIDER_LABELS
        .iter()
        .find(|(source, _)| source.eq_ignore_ascii_case(provider))
        .map_or(provider, |(_, label)| *label)
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
    let mut agents = build_agents(inputs);
    with_child_activity(&mut agents);
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

/// Trim a model value and exclude empty values and angle-bracket placeholders.
fn concrete_model(model: Option<&str>) -> Option<String> {
    model
        .map(str::trim)
        .filter(|model| !model.is_empty() && !(model.starts_with('<') && model.ends_with('>')))
        .map(str::to_owned)
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
            // An ended assignment keeps its row without a terminal: the daemon drops the
            // terminal with the session, and the row must still roll up as no-live. Its
            // empty terminal id matches no pane and gives focus nothing to open.
            let (terminal_id, backend, terminal_state) = match entry.terminal.as_ref() {
                Some(terminal) => (
                    terminal.terminal_id.as_str(),
                    terminal.backend,
                    terminal.state.clone(),
                ),
                None if entry.lifecycle_status.as_deref() == Some("dead") => {
                    ("", Backend::default(), None)
                }
                None => return None,
            };
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
                .find(|pane| pane.terminal_id == terminal_id);
            // The roster places every entry on the hub; the fetched rows only
            // cover the local windows, so they are the fallback.
            let project_id = entry
                .project_id
                .as_deref()
                .filter(|project| !project.is_empty())
                .or(session.map(|(project, _)| project))
                .or(run.map(|(project, _)| project))
                .or(inputs.focused_project)
                .unwrap_or_default();
            let provider = entry
                .provider
                .clone()
                .or_else(|| run.and_then(|(_, run)| run.provider.clone()))
                .or_else(|| session.and_then(|(_, session)| session.source.clone()))
                .filter(|provider| !provider.is_empty());
            // The name ladder: the agent definition, then the tmux name, then the
            // pane's own name, and the short terminal id for a roster entry with
            // no pane open. The provider is no rung: it rides on the row's second
            // line with the model.
            let agent_definition_name = entry
                .definition
                .clone()
                .filter(|name| !name.trim().is_empty())
                .or_else(|| {
                    run.and_then(|(_, run)| run.agent_name.clone())
                        .filter(|name| !name.trim().is_empty())
                });
            let anchor = if terminal_id.is_empty() {
                entry
                    .session_id
                    .as_deref()
                    .or(entry.run_id.as_deref())
                    .unwrap_or(&entry.entry_id)
            } else {
                terminal_id
            };
            let name = agent_definition_name
                .clone()
                .or_else(|| {
                    entry
                        .tmux_session_name
                        .clone()
                        .filter(|name| !name.is_empty())
                })
                .or_else(|| pane.map(|pane| pane.display_name().to_string()))
                .unwrap_or_else(|| short_terminal_id(anchor).to_string());
            let machine_id = [
                entry.machine_id.as_deref(),
                session.and_then(|(_, session)| session.machine_id.as_deref()),
                run.and_then(|(_, run)| run.machine_id.as_deref()),
            ]
            .into_iter()
            .flatten()
            .find(|machine| !machine.is_empty())
            .unwrap_or(inputs.local_machine)
            .to_string();
            let managed = entry.run_id.is_some() || entry.entry_id.starts_with("run:");
            Some(AgentEntry {
                entry_id: entry.entry_id.clone(),
                project_id: project_id.to_string(),
                machine_id,
                terminal_id: terminal_id.to_string(),
                backend,
                name,
                agent_definition_name,
                provider: provider.unwrap_or_default(),
                model: concrete_model(entry.model.as_deref())
                    .or_else(|| concrete_model(run.and_then(|(_, run)| run.model.as_deref())))
                    .or_else(|| {
                        concrete_model(session.and_then(|(_, session)| session.model.as_deref()))
                    }),
                model_display_name: concrete_model(entry.model_display_name.as_deref()),
                task_ref: entry.task.as_ref().and_then(|task| task.reference.clone()),
                task_id: entry.task.as_ref().map(|task| task.id.clone()),
                task_title: entry.task.as_ref().and_then(|task| task.title.clone()),
                task_claim_held: entry.task.as_ref().is_some_and(|task| task.claim_holder),
                status_line: entry.status_line.clone(),
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
                context_percent: entry.context_percent,
                tokens_used: entry.tokens_used,
                managed,
                worktree_id: run
                    .and_then(|(_, run)| run.worktree_id.clone())
                    .or_else(|| session.and_then(|(_, session)| session.worktree_id.clone())),
                lifecycle_status: entry.lifecycle_status.clone(),
                terminal_state,
                state: agent_state(entry),
                attention: entry.attention.clone(),
                last_activity_at: entry.last_activity_at.clone(),
                sandbox: SandboxState::resolve(
                    session.and_then(|(_, session)| session.sandbox_enabled),
                    run.and_then(|(_, run)| run.sandbox.as_ref()?.enforced),
                    managed,
                ),
            })
        })
        .collect()
}

/// An agent between turns waiting on a child run it started stays active while
/// that run works; a chain of waiting parents follows its working leaf. Other
/// states hold, so an operator pause or a needs-you prompt is not overridden.
fn with_child_activity(agents: &mut [AgentEntry]) {
    loop {
        let parents_of_working: BTreeSet<String> = agents
            .iter()
            .filter(|agent| agent.state == RowState::Working)
            .filter_map(|agent| agent.parent_session_id.clone())
            .collect();
        let mut changed = false;
        for agent in agents.iter_mut() {
            let waiting = agent
                .session_id
                .as_ref()
                .is_some_and(|id| parents_of_working.contains(id));
            let between_turns =
                agent.state == RowState::Paused && !is_held(agent.lifecycle_status.as_deref());
            if between_turns && waiting {
                agent.state = RowState::Working;
                changed = true;
            }
        }
        if !changed {
            return;
        }
    }
}

/// Whether `agent` works in the worktree: bound by its worktree id, or, when
/// it has none, running the worktree's task. An epic's integration tree has no
/// session of its own, so its team lead is the unbound agent on its task.
fn is_bound_to(agent: &AgentEntry, worktree_id: &str, task_id: Option<&str>) -> bool {
    match agent.worktree_id.as_deref() {
        Some(id) => id == worktree_id,
        None => task_id.is_some() && agent.task_id.as_deref() == task_id,
    }
}

/// A dead agent bound to a tree only by its task is a previous lead once
/// another such agent runs: the tree shows its live lead's state.
fn superseded_lead<'a>(
    agent: &AgentEntry,
    mut bound: impl Iterator<Item = &'a AgentEntry>,
) -> bool {
    agent.worktree_id.is_none()
        && agent.state == RowState::NoLive
        && bound.any(|other| other.worktree_id.is_none() && other.state != RowState::NoLive)
}

fn superseded_in_worktrees(
    agent: &AgentEntry,
    agents: &[AgentEntry],
    worktrees: &[WorktreeEntry],
) -> bool {
    if agent.worktree_id.is_some() || agent.state != RowState::NoLive {
        return false;
    }
    worktrees.iter().any(|worktree| {
        let task_id = worktree.task_id.as_deref();
        is_bound_to(agent, &worktree.worktree_id, task_id)
            && superseded_lead(
                agent,
                agents.iter().filter(|other| {
                    other.project_id == agent.project_id
                        && is_bound_to(other, &worktree.worktree_id, task_id)
                }),
            )
    })
}

/// Logs each agent that just came to need the operator when its definition is
/// not the default one, so a non-default seat asking for attention shows in the
/// log. An agent that needed you in `previous` is not logged again.
pub fn log_needs_you_transitions(previous: &SidebarModel, current: &SidebarModel) {
    for agent in current
        .agents
        .iter()
        .filter(|agent| agent.state == RowState::Attention)
    {
        let already = previous
            .agents
            .iter()
            .any(|old| old.entry_id == agent.entry_id && old.state == RowState::Attention);
        let definition = agent.agent_definition_name.as_deref().unwrap_or("default");
        if already || definition == "default" {
            continue;
        }
        // A roster-only session has no fetched ref; its id still names it.
        let session = agent
            .session_ref
            .as_deref()
            .or(agent.session_id.as_deref())
            .unwrap_or("");
        tracing::warn!(
            entry_id = %agent.entry_id,
            session_ref = session,
            agent_definition = definition,
            "agent needs you"
        );
    }
}

fn project_entry(row: &ProjectRow, inputs: &SidebarInputs, agents: &[AgentEntry]) -> ProjectEntry {
    let status = inputs.rows.statuses.get(&row.id);
    let own: Vec<&AgentEntry> = agents
        .iter()
        .filter(|agent| agent.project_id == row.id)
        .collect();
    let worktrees: Vec<WorktreeEntry> = inputs
        .rows
        .worktrees
        .iter()
        .filter(|worktree| worktree.project_id == row.id)
        .map(|worktree| {
            let bound: Vec<&AgentEntry> = own
                .iter()
                .copied()
                .filter(|agent| is_bound_to(agent, &worktree.id, worktree.task_id.as_deref()))
                .collect();
            WorktreeEntry {
                worktree_id: worktree.id.clone(),
                branch: worktree.branch_name.clone(),
                path: PathBuf::from(&worktree.worktree_path),
                task_ref: bound
                    .iter()
                    .find_map(|agent| agent.task_ref.clone())
                    .or_else(|| worktree.task_id.clone()),
                task_id: worktree.task_id.clone(),
                role: worktree.workspace_role.clone(),
                state: rollup(
                    bound
                        .iter()
                        .filter(|agent| !superseded_lead(agent, bound.iter().copied()))
                        .map(|agent| agent.state),
                ),
            }
        })
        .collect();
    let own: Vec<_> = own
        .into_iter()
        .filter(|agent| !superseded_in_worktrees(agent, agents, &worktrees))
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
        state: rollup(own.iter().map(|agent| agent.state)),
        needs_you: own
            .iter()
            .filter(|agent| agent.state == RowState::Attention)
            .count(),
        no_live: own
            .iter()
            .filter(|agent| agent.state == RowState::NoLive)
            .count(),
    }
}

/// The bound agent a click on `worktree_id`'s state dot shows: the one
/// whose state the dot draws, the first in roster order on a tie (#23280).
pub fn worktree_focus_target<'a>(
    model: &'a SidebarModel,
    worktree_id: &str,
) -> Option<&'a AgentEntry> {
    let task_id = model
        .projects
        .iter()
        .flat_map(|project| project.worktrees.iter())
        .find(|worktree| worktree.worktree_id == worktree_id)
        .and_then(|worktree| worktree.task_id.as_deref());
    model
        .agents
        .iter()
        // An assignment with no terminal has no pane to focus; the row opens the worktree.
        .filter(|agent| !agent.terminal_id.is_empty() && is_bound_to(agent, worktree_id, task_id))
        .min_by_key(|agent| Reverse(urgency(agent.state)))
}

/// Needs you over active over paused over no live agents over blank.
pub fn urgency(state: RowState) -> u8 {
    match state {
        RowState::Attention => 4,
        RowState::Working => 3,
        RowState::Paused => 2,
        RowState::NoLive => 1,
        RowState::Blank => 0,
    }
}

/// A container's state: the most urgent member, and blank with no members.
pub fn rollup(states: impl IntoIterator<Item = RowState>) -> RowState {
    states
        .into_iter()
        .max_by_key(|state| urgency(*state))
        .unwrap_or(RowState::Blank)
}

/// The state of one roster entry. Output in its pane does not make it active.
pub fn agent_state(entry: &RosterEntry) -> RowState {
    let terminal_state = entry
        .terminal
        .as_ref()
        .and_then(|terminal| terminal.state.as_deref());
    resolve_state(
        entry
            .attention
            .as_ref()
            .is_some_and(|attention| attention.kind.as_deref() == Some("actionable")),
        is_orphaned(terminal_state),
        entry.lifecycle_status.as_deref(),
    )
}

/// `orphaned` is the daemon's state for a row whose host is gone.
pub fn is_orphaned(terminal_state: Option<&str>) -> bool {
    terminal_state == Some("orphaned")
}

fn resolve_state(blocked: bool, orphaned: bool, lifecycle_status: Option<&str>) -> RowState {
    // An ended assignment holds no live work, whatever attention it still carries.
    if lifecycle_status == Some("dead") {
        return RowState::NoLive;
    }
    if blocked
        || lifecycle_status
            .is_some_and(|status| matches!(status, "awaiting_input" | "awaiting_approval"))
    {
        return RowState::Attention;
    }
    if orphaned {
        return RowState::NoLive;
    }
    if lifecycle_status.is_some_and(|status| matches!(status, "running" | "active")) {
        return RowState::Working;
    }
    // A live agent that is neither working nor waiting on a human is paused: an
    // operator hold, a user interrupt, or a routine turn end ("idle", "waiting").
    // The daemon gives every live session and run a status, so an entry without
    // one carries no agent state, such as a bare shell, and stays blank.
    if lifecycle_status.is_some() {
        RowState::Paused
    } else {
        RowState::Blank
    }
}

/// An operator pause or a user interrupt, as opposed to a routine turn end; a
/// child run's activity never overrides a hold.
fn is_held(lifecycle_status: Option<&str>) -> bool {
    matches!(lifecycle_status, Some("paused" | "interrupted"))
}

/// Roster and attention reads a live event asks for. Applying the event only
/// sets these; the loop drains them into coalesced jobs, so no event awaits a
/// daemon read.
#[derive(Debug, Default)]
pub(super) struct PendingRefetch {
    /// One roster read per project. A scoped read for a project that is no
    /// longer focused is dropped when it lands.
    pub(super) rosters: BTreeSet<String>,
    pub(super) attention: bool,
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

impl PendingSidebar {
    /// Every row set, for a lagged event receiver: the render tick's refetch
    /// job reads them beside the loop. Reading them inline held the loop for
    /// the slowest daemon answer, and the frames that piled up meanwhile
    /// lagged the receiver again, so the screen never drew (#22747).
    pub(super) fn everything(project_rows: Vec<String>) -> Self {
        Self {
            projects: true,
            project_rows: project_rows.into_iter().collect(),
            sessions: true,
            session_rows: BTreeSet::new(),
            roster: true,
        }
    }
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

    pub fn roster_entry_ids(&self) -> Vec<String> {
        self.attention
            .entries
            .iter()
            .map(|entry| entry.entry_id.clone())
            .collect()
    }

    pub fn attention_entry_ids(&self) -> Vec<String> {
        self.attention
            .entries
            .iter()
            .filter(|entry| agent_state(entry) == RowState::Attention)
            .map(|entry| entry.entry_id.clone())
            .collect()
    }

    pub fn set_local_machine(&mut self, machine_id: impl Into<String>) {
        self.local_machine = machine_id.into();
        self.rebuild_sidebar();
    }

    pub(super) fn rebuild_sidebar(&mut self) {
        let previous = std::mem::take(&mut self.sidebar);
        self.sidebar = self.build_sidebar();
        log_needs_you_transitions(&previous, &self.sidebar);
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

    /// Upsert the roster entry an `attention_changed` event names. The event
    /// carries the entry under `metadata` when the daemon built one, else its
    /// flat fields double as the entry; only an explicit `state` moves the
    /// blocked flag, so a metadata-only event keeps what the roster said.
    pub(super) fn note_attention_event(&mut self, payload: &Value) {
        // The event carries the attention alone; lifecycle_status and the
        // terminal state arrive with the roster, so ask for it again.
        self.pending_sidebar.roster = true;
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
