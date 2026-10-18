//! Snapshot stored at node/agents.json.
use crate::node_commands::{ExitReason, ReportExit, WorkspaceMode};
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
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkspaceRecord {
    pub mode: WorkspaceMode,
    pub path: PathBuf,
    pub branch_name: String,
    pub base_branch: String,
    pub source_root: PathBuf,
    pub reused: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExitRecord {
    pub exit: ReportExit,
    pub ended_at_ms: u64,
}

impl AgentState {
    /// Call under NodeAgents::state's mutex, held through the durable rename.
    pub fn commit(&self, home: &Path, changed: &Notify) -> Result<()> {
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
        result.with_context(|| format!("commit node agent state {}", path.display()))?;
        changed.notify_one();
        Ok(())
    }

    pub fn next_revision(&mut self, home: &Path, changed: &Notify) -> Result<u64> {
        self.revision = self
            .revision
            .checked_add(1)
            .context("node report revision exhausted")?;
        self.commit(home, changed)?;
        Ok(self.revision)
    }

    pub fn live_and_accepted(&self) -> usize {
        self.runs.len()
    }

    pub fn prune_cancelled(&mut self, now_ms: u64) {
        const RETENTION_MS: u64 = 24 * 60 * 60 * 1_000;
        self.cancelled
            .retain(|_, cancelled_at| now_ms.saturating_sub(*cancelled_at) <= RETENTION_MS);
    }

    pub fn to_removals(&mut self, run_id: &str) -> Option<WorkspaceRecord> {
        let workspace = self.runs.get_mut(run_id)?.workspace.take()?;
        if workspace.reused {
            return None;
        }
        self.removals.insert(run_id.to_owned(), workspace.clone());
        Some(workspace)
    }

    pub fn recover_after_restart(&mut self) {
        let interrupted: Vec<_> = self
            .runs
            .iter()
            .filter(|(_, run)| run.phase != RunPhase::Launched)
            .map(|(run_id, _)| run_id.clone())
            .collect();
        let ended_at_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_millis().min(u64::MAX as u128) as u64)
            .unwrap_or(0);
        for run_id in interrupted {
            self.to_removals(&run_id);
            if let Some(run) = self.runs.remove(&run_id) {
                self.exits.push(ExitRecord {
                    exit: ReportExit {
                        run_id,
                        terminal_id: run.terminal_id,
                        launched: false,
                        exit_code: None,
                        reason: ExitReason::PrepareFailed,
                        workspace: None,
                        sandbox: None,
                    },
                    ended_at_ms,
                });
            }
        }
        self.exits.sort_by_key(|exit| exit.ended_at_ms);
    }
}
