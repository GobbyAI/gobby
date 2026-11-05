//! Snapshot stored at node/agents.json.
use crate::node_commands::{ExitReason, ReportExit, ReportMode, ReportWorkspace, WorkspaceMode};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{ErrorKind, Write},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::sync::Notify;

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct AgentState {
    pub revision: u64,
    pub hub_epoch: Option<u64>,
    pub host_epoch: u64,
    pub host_epoch_id: Option<String>,
    pub runs: BTreeMap<String, RunRecord>,
    pub removals: BTreeMap<String, WorkspaceRecord>,
    pub cancelled: BTreeMap<String, u64>,
    pub exits: Vec<ExitRecord>,
    /// Host exits seen while a run was still `Preparing`. `record_launched` settles them once
    /// the commit succeeds, so a fast exit is not lost before the run is owned.
    #[serde(default)]
    pub pending_exits: BTreeMap<String, Option<i64>>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunRecord {
    pub terminal_id: String,
    pub phase: RunPhase,
    pub accepted_at_ms: u64,
    pub workspace: Option<WorkspaceRecord>,
    pub host_terminal_id: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RunPhase {
    Accepted,
    Preparing,
    Launched,
    /// The run ended and its exit awaits a report. The record keeps the launched workspace
    /// until a report carrying the exit gets 200.
    Exited,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkspaceRecord {
    pub mode: WorkspaceMode,
    pub path: PathBuf,
    pub branch_name: String,
    pub base_branch: Option<String>,
    pub source_root: PathBuf,
    pub reused: bool,
}

impl WorkspaceRecord {
    pub(super) fn report(&self) -> ReportWorkspace {
        ReportWorkspace {
            mode: match self.mode {
                WorkspaceMode::Worktree => ReportMode::Worktree,
                WorkspaceMode::Clone => ReportMode::Clone,
            },
            path: self.path.display().to_string(),
            branch_name: self.branch_name.clone(),
            base_branch: self.base_branch.clone(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExitRecord {
    pub exit: ReportExit,
    pub ended_at_ms: u64,
}

impl AgentState {
    /// Persists the state and wakes the report loop. Call under NodeAgents::state's mutex.
    pub fn commit(&self, home: &Path, changed: &Notify) -> Result<()> {
        self.persist(home)?;
        changed.notify_one();
        Ok(())
    }

    /// Persists the state without waking the report loop, so a report's own bookkeeping never
    /// starts another report. Call under NodeAgents::state's mutex, held through the durable rename.
    pub fn persist(&self, home: &Path) -> Result<()> {
        let directory = home.join("node");
        let path = directory.join("agents.json");
        let temporary = directory.join("agents.json.tmp");
        let bytes = serde_json::to_vec(self).context("serialize node agent state")?;
        let result = (|| -> Result<()> {
            let mut builder = fs::DirBuilder::new();
            builder.recursive(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
                builder.mode(0o700);
                builder.create(&directory)?;
                fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))?;
            }
            #[cfg(not(unix))]
            builder.create(&directory)?;
            // A singleton owns this snapshot. Discard only an interrupted temp
            // write, never the last committed state, and never follow a temp link.
            match fs::remove_file(&temporary) {
                Ok(()) => (),
                Err(error) if error.kind() == ErrorKind::NotFound => (),
                Err(error) => return Err(error.into()),
            }
            let mut options = OpenOptions::new();
            options.write(true).create_new(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options.mode(0o600);
            }
            let mut file = options.open(&temporary)?;
            file.write_all(&bytes)?;
            file.sync_all()?;
            fs::rename(&temporary, &path)?;
            File::open(&directory)?.sync_all()?;
            Ok(())
        })();
        result.with_context(|| format!("commit node agent state {}", path.display()))
    }

    /// Takes the next report revision and persists it before the report is built.
    pub fn take_report_revision(&mut self, home: &Path) -> Result<u64> {
        self.revision = self
            .revision
            .checked_add(1)
            .context("node report revision exhausted")?;
        self.persist(home)?;
        Ok(self.revision)
    }

    pub fn next_revision(&mut self, home: &Path, changed: &Notify) -> Result<u64> {
        let revision = self.take_report_revision(home)?;
        changed.notify_one();
        Ok(revision)
    }

    /// Runs that hold capacity. Exited records await a report and do not count.
    pub fn live_and_accepted(&self) -> usize {
        self.runs
            .values()
            .filter(|run| {
                matches!(
                    run.phase,
                    RunPhase::Accepted | RunPhase::Preparing | RunPhase::Launched
                )
            })
            .count()
    }

    pub fn prune_cancelled(&mut self, now_ms: u64) {
        const RETENTION_MS: u64 = 24 * 60 * 60 * 1_000;
        self.cancelled
            .retain(|_, cancelled_at| now_ms.saturating_sub(*cancelled_at) <= RETENTION_MS);
    }

    /// Queues the run's workspace for removal and returns it. The run keeps its record, so the
    /// exit report still carries the workspace; callers drop the run themselves.
    pub fn to_removals(&mut self, run_id: &str) -> Option<WorkspaceRecord> {
        let workspace = self.runs.get(run_id)?.workspace.clone()?;
        if workspace.reused {
            return None;
        }
        self.removals.insert(run_id.to_owned(), workspace.clone());
        Some(workspace)
    }

    /// Forgets an acknowledged exit's run. Its workspace stays on disk: only its completion
    /// owner's `delete_worktree` retires it.
    pub fn acknowledge_exit(&mut self, run_id: &str) {
        if self
            .runs
            .get(run_id)
            .is_some_and(|run| run.phase == RunPhase::Exited)
        {
            self.runs.remove(run_id);
        }
    }

    /// Why a run that ended on its own is recorded: `cancelled` when a cancel record exists.
    pub fn exit_reason(&self, run_id: &str) -> ExitReason {
        if self.cancelled.contains_key(run_id) {
            ExitReason::Cancelled
        } else {
            ExitReason::Exited
        }
    }

    pub fn recover_after_restart(&mut self) {
        let interrupted: Vec<_> = self
            .runs
            .iter()
            .filter(|(_, run)| matches!(run.phase, RunPhase::Accepted | RunPhase::Preparing))
            .map(|(run_id, _)| run_id.clone())
            .collect();
        let ended_at_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_millis().min(u64::MAX as u128) as u64)
            .unwrap_or(0);
        for run_id in interrupted {
            let pending = self.pending_exits.remove(&run_id);
            let Some(run) = self.runs.get(&run_id) else {
                continue;
            };
            let terminal_id = run.terminal_id.clone();
            let launched_workspace = run.workspace.as_ref().map(WorkspaceRecord::report);
            let exit = match pending {
                // A host exit seen before the commit reply means the child ran and ended. The
                // launched workspace stays with the run until a report carrying the exit gets 200.
                Some(exit_code) => {
                    let reason = self.exit_reason(&run_id);
                    if let Some(run) = self.runs.get_mut(&run_id) {
                        run.phase = RunPhase::Exited;
                    }
                    ReportExit {
                        run_id,
                        terminal_id,
                        launched: true,
                        exit_code,
                        reason,
                        workspace: launched_workspace,
                        sandbox: None,
                    }
                }
                // Nothing launched: the tree stays on disk for its completion owner, and the
                // exit names no workspace.
                None => {
                    self.runs.remove(&run_id);
                    ReportExit {
                        run_id,
                        terminal_id,
                        launched: false,
                        exit_code: None,
                        reason: ExitReason::PrepareFailed,
                        workspace: None,
                        sandbox: None,
                    }
                }
            };
            self.exits.push(ExitRecord { exit, ended_at_ms });
        }
        self.exits.sort_by_key(|exit| exit.ended_at_ms);
    }
}
