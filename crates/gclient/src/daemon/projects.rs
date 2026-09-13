//! Typed rows behind the sidebar: projects, source status, worktrees,
//! sessions, and agent runs. Every row ignores unknown fields and defaults
//! the ones the daemon may omit, so a route can grow without breaking parse.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// A `/api/projects` row. `display_name` is `Personal` for the `_personal`
/// project; `checkout` is set only where this machine has the project.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct ProjectRow {
    pub id: String,
    pub name: String,
    pub display_name: String,
    pub checkout: Option<Checkout>,
    pub session_count: u64,
    pub last_activity_at: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Checkout {
    pub machine_id: String,
    pub root_path: String,
}

/// `/api/source-control/status?project_id=`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct SourceStatus {
    pub current_branch: Option<String>,
    pub ahead: Option<u32>,
    pub behind: Option<u32>,
    pub repo_path: Option<String>,
    pub worktree_count: u32,
}

/// One entry of `/api/source-control/worktrees?project_id=`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct WorktreeRow {
    pub id: String,
    pub project_id: String,
    pub machine_id: Option<String>,
    pub task_id: Option<String>,
    pub branch_name: String,
    pub worktree_path: String,
    pub base_branch: Option<String>,
    pub agent_session_id: Option<String>,
    pub status: String,
    pub workspace_role: String,
}

/// One entry of `/api/sessions?project_id=`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct SessionRow {
    pub id: String,
    #[serde(rename = "ref")]
    pub reference: Option<String>,
    pub title: Option<String>,
    pub source: Option<String>,
    pub status: String,
    pub git_branch: Option<String>,
    pub machine_id: Option<String>,
    pub agent_run_id: Option<String>,
    pub model: Option<String>,
    pub reasoning_effort: Option<String>,
    /// The session that spawned this one, for a child session of an agent run.
    pub parent_session_id: Option<String>,
}

impl SessionRow {
    pub fn reasoning_effort(&self) -> Option<&str> {
        self.reasoning_effort
            .as_deref()
            .filter(|effort| !effort.is_empty())
    }
}

/// One entry of `/api/agents/runs?project_id=`.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct RunRow {
    pub run_id: String,
    pub agent_name: Option<String>,
    pub provider: Option<String>,
    pub model: Option<String>,
    pub status: String,
    pub task_id: Option<String>,
    pub terminal_id: Option<String>,
    pub worktree_id: Option<String>,
    pub machine_id: Option<String>,
    /// The session that spawned the run: the sidebar nests the run under it.
    pub parent_session_id: Option<String>,
    pub child_session_id: Option<String>,
    pub effective_reasoning_effort: Option<String>,
    pub requested_reasoning_effort: Option<String>,
}

impl RunRow {
    /// The reasoning effort the run works at, the requested one until the
    /// daemon resolved it.
    pub fn reasoning_effort(&self) -> Option<&str> {
        self.effective_reasoning_effort
            .as_deref()
            .or(self.requested_reasoning_effort.as_deref())
            .filter(|effort| !effort.is_empty())
    }
}

/// Everything the sidebar model is built from besides the roster and panes.
/// `statuses`, `sessions`, and `runs` are keyed by project id.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct SidebarRows {
    pub projects: Vec<ProjectRow>,
    pub statuses: BTreeMap<String, SourceStatus>,
    pub worktrees: Vec<WorktreeRow>,
    pub sessions: BTreeMap<String, Vec<SessionRow>>,
    pub runs: BTreeMap<String, Vec<RunRow>>,
}
