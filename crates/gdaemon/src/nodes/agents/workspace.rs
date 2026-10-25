//! Workspace lifecycle for node-run agents: plan, prepare, remove, retry removals.

use super::{NodeAgents, repair::RepairEnv, state::WorkspaceRecord};
use crate::node_commands::SpawnWorkspace;
use anyhow::Result;
use std::path::Path;

/// Resolves the workspace record a spawn asks for. `None` means `none` mode,
/// which runs in `source_root` with no record.
pub fn plan_workspace(
    run_id: &str,
    project_id: &str,
    source_root: &Path,
    workspace: &SpawnWorkspace,
    home: &Path,
) -> Result<Option<WorkspaceRecord>> {
    let _ = (run_id, project_id, source_root, workspace, home);
    todo!("plan_workspace: not implemented yet")
}

/// Commits the run's `preparing` phase with `record`, then creates the
/// workspace (unless reused) and repairs it.
pub async fn prepare(
    agents: &NodeAgents,
    run_id: &str,
    record: &WorkspaceRecord,
    provider: &str,
    env: &RepairEnv<'_>,
) -> Result<()> {
    let _ = (agents, run_id, record, provider, env);
    todo!("prepare: not implemented yet")
}

/// Removes a workspace and its branch. A missing path or branch counts as removed.
pub async fn remove(record: &WorkspaceRecord) -> Result<()> {
    let _ = record;
    todo!("remove: not implemented yet")
}

/// Tries every pending removal and deletes each record whose removal succeeds.
/// A failed removal is retained for the next call, not returned as an error.
pub async fn retry_removals(agents: &NodeAgents) -> Result<()> {
    let _ = agents;
    todo!("retry_removals: not implemented yet")
}
