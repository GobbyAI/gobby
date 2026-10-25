//! Workspace lifecycle for node-run agents: plan, prepare, remove, retry removals.

use super::{
    NodeAgents,
    repair::{self, RepairEnv},
    state::{RunPhase, WorkspaceRecord},
};
use crate::node_commands::{SpawnMode, SpawnWorkspace, WorkspaceMode};
use anyhow::{Context, Result, ensure};
use std::{
    fs,
    path::Path,
    process::{Command as StdCommand, Stdio},
};
use tokio::process::Command;

/// Resolves the workspace record a spawn asks for. `None` means `none` mode,
/// which runs in `source_root` with no record.
pub fn plan_workspace(
    run_id: &str,
    project_id: &str,
    source_root: &Path,
    workspace: &SpawnWorkspace,
    home: &Path,
) -> Result<Option<WorkspaceRecord>> {
    let mode = match workspace.mode {
        SpawnMode::None => return Ok(None),
        SpawnMode::Existing => {
            return existing_record(project_id, source_root, workspace, home).map(Some);
        }
        SpawnMode::Worktree => WorkspaceMode::Worktree,
        SpawnMode::Clone => WorkspaceMode::Clone,
    };
    ensure_component("run id", run_id)?;
    ensure_component("project id", project_id)?;
    let branch = workspace
        .branch_name
        .as_deref()
        .context("worktree and clone workspaces need a branch name")?;
    let branch_name = if branch.contains(run_id) {
        branch.to_owned()
    } else {
        format!("{branch}-{run_id}")
    };
    let base_branch = match &workspace.base_branch {
        Some(base) => base.clone(),
        None => git_sync(source_root, &["rev-parse", "--abbrev-ref", "HEAD"])?,
    };
    Ok(Some(WorkspaceRecord {
        mode,
        path: home.join(root_dir(mode)).join(project_id).join(run_id),
        branch_name,
        base_branch: Some(base_branch),
        source_root: source_root.to_owned(),
        reused: false,
    }))
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
    {
        let mut state = agents.state.lock().await;
        let run = state
            .runs
            .get_mut(run_id)
            .with_context(|| format!("run {run_id} is not accepted"))?;
        run.phase = RunPhase::Preparing;
        run.workspace = Some(record.clone());
        state.commit(&agents.home, &agents.changed)?;
    }
    if !record.reused {
        create(record).await?;
    }
    repair::repair(&record.source_root, &record.path, provider, env).await
}

/// Removes a workspace and its branch. A missing path or branch counts as removed.
pub async fn remove(record: &WorkspaceRecord) -> Result<()> {
    match record.mode {
        WorkspaceMode::Worktree => remove_worktree(record).await,
        WorkspaceMode::Clone => {
            if record.path.exists() {
                fs::remove_dir_all(&record.path)
                    .with_context(|| format!("remove clone {}", record.path.display()))?;
            }
            Ok(())
        }
    }
}

/// Tries every pending removal and deletes each record whose removal succeeds.
/// A failed removal is retained for the next call, not returned as an error.
pub async fn retry_removals(agents: &NodeAgents) -> Result<()> {
    let pending: Vec<(String, WorkspaceRecord)> = agents
        .state
        .lock()
        .await
        .removals
        .iter()
        .map(|(run_id, record)| (run_id.clone(), record.clone()))
        .collect();
    let mut removed = Vec::new();
    for (run_id, record) in pending {
        match remove(&record).await {
            Ok(()) => removed.push(run_id),
            Err(error) => {
                eprintln!("gdaemon node: keeping removal record for run {run_id}: {error:#}");
            }
        }
    }
    if removed.is_empty() {
        return Ok(());
    }
    let mut state = agents.state.lock().await;
    for run_id in &removed {
        state.removals.remove(run_id);
    }
    state.commit(&agents.home, &agents.changed)
}

fn existing_record(
    project_id: &str,
    source_root: &Path,
    workspace: &SpawnWorkspace,
    home: &Path,
) -> Result<WorkspaceRecord> {
    ensure_component("project id", project_id)?;
    let given = workspace
        .path
        .as_deref()
        .context("an existing workspace needs its path")?;
    let path = fs::canonicalize(given)
        .with_context(|| format!("existing workspace {given} is missing or unreadable"))?;
    ensure!(
        path.is_dir(),
        "existing workspace {} is not a directory",
        path.display()
    );
    let home = fs::canonicalize(home).context("canonicalize the gobby home")?;
    let parent = path.parent();
    let mode = [WorkspaceMode::Worktree, WorkspaceMode::Clone]
        .into_iter()
        .find(|mode| parent == Some(home.join(root_dir(*mode)).join(project_id).as_path()))
        .with_context(|| {
            format!(
                "existing workspace {} is outside this project's worktrees and clones",
                path.display()
            )
        })?;
    Ok(WorkspaceRecord {
        mode,
        path,
        branch_name: workspace
            .branch_name
            .clone()
            .context("an existing workspace needs its branch name")?,
        base_branch: None,
        source_root: source_root.to_owned(),
        reused: true,
    })
}

async fn create(record: &WorkspaceRecord) -> Result<()> {
    let base = record
        .base_branch
        .as_deref()
        .context("a new workspace needs a base branch")?;
    // Resolve the base before any filesystem effect so a bad base leaves nothing behind.
    let commit = git_output(
        &record.source_root,
        &[
            "rev-parse",
            "--verify",
            "--quiet",
            &format!("{base}^{{commit}}"),
        ],
    )
    .await
    .with_context(|| format!("base {base} does not resolve to a commit"))?;
    let commit = commit.trim();
    let parent = record
        .path
        .parent()
        .context("workspace path has no parent")?;
    fs::create_dir_all(parent).with_context(|| format!("create {}", parent.display()))?;
    let path = path_str(&record.path)?;
    match record.mode {
        WorkspaceMode::Worktree => {
            git_output(
                &record.source_root,
                &[
                    "worktree",
                    "add",
                    "-q",
                    "-b",
                    &record.branch_name,
                    path,
                    commit,
                ],
            )
            .await?;
        }
        WorkspaceMode::Clone => {
            let source = path_str(&record.source_root)?;
            git_output(parent, &["clone", "-q", source, path]).await?;
            git_output(
                &record.path,
                &["checkout", "-q", "-b", &record.branch_name, commit],
            )
            .await?;
        }
    }
    Ok(())
}

async fn remove_worktree(record: &WorkspaceRecord) -> Result<()> {
    let path = path_str(&record.path)?;
    if record.path.exists()
        && let Err(error) = git_output(
            &record.source_root,
            &["worktree", "remove", "--force", path],
        )
        .await
    {
        // A checkout that still holds `.git` is not removable yet: keep the record.
        if record.path.join(".git").exists() {
            return Err(error);
        }
        fs::remove_dir_all(&record.path).with_context(|| format!("remove worktree {path}"))?;
    }
    git_output(&record.source_root, &["worktree", "prune"]).await?;
    let branch_ref = format!("refs/heads/{}", record.branch_name);
    if git_succeeds(
        &record.source_root,
        &["show-ref", "--verify", "--quiet", &branch_ref],
    )
    .await?
    {
        git_output(&record.source_root, &["branch", "-D", &record.branch_name]).await?;
    }
    Ok(())
}

fn root_dir(mode: WorkspaceMode) -> &'static str {
    match mode {
        WorkspaceMode::Worktree => "worktrees",
        WorkspaceMode::Clone => "clones",
    }
}

/// Rejects a value that would escape its root when joined into a workspace path.
fn ensure_component(what: &str, value: &str) -> Result<()> {
    let mut parts = Path::new(value).components();
    ensure!(
        matches!(parts.next(), Some(std::path::Component::Normal(_))) && parts.next().is_none(),
        "{what} {value:?} must be a single path component"
    );
    Ok(())
}

fn path_str(path: &Path) -> Result<&str> {
    path.to_str()
        .with_context(|| format!("path {} is not UTF-8", path.display()))
}

pub(super) async fn git_output(dir: &Path, args: &[&str]) -> Result<String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(dir)
        .args(args)
        .kill_on_drop(true)
        .output()
        .await
        .with_context(|| format!("run git {} in {}", args.join(" "), dir.display()))?;
    ensure!(
        output.status.success(),
        "git {} failed in {}: {}",
        args.join(" "),
        dir.display(),
        String::from_utf8_lossy(&output.stderr).trim()
    );
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

pub(super) async fn git_succeeds(dir: &Path, args: &[&str]) -> Result<bool> {
    let status = Command::new("git")
        .arg("-C")
        .arg(dir)
        .args(args)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .status()
        .await
        .with_context(|| format!("run git {} in {}", args.join(" "), dir.display()))?;
    Ok(status.success())
}

fn git_sync(dir: &Path, args: &[&str]) -> Result<String> {
    let output = StdCommand::new("git")
        .arg("-C")
        .arg(dir)
        .args(args)
        .output()
        .with_context(|| format!("run git {} in {}", args.join(" "), dir.display()))?;
    ensure!(
        output.status.success(),
        "git {} failed in {}: {}",
        args.join(" "),
        dir.display(),
        String::from_utf8_lossy(&output.stderr).trim()
    );
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}
