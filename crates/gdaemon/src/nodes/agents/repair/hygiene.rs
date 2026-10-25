//! Hides Gobby-generated isolation metadata from the workspace's local git status.

use super::write_atomic;
use crate::nodes::agents::workspace::{git_output, git_succeeds};
use anyhow::{Context, Result};
use std::{
    fs,
    path::{Path, PathBuf},
};

const GENERATED_PATHS: [&str; 5] = [
    ".mcp.json",
    ".claude/",
    ".codex/",
    ".factory/hooks/hooks.json",
    ".gobby/isolation.json",
];
const PROJECT_JSON: &str = ".gobby/project.json";

/// Excludes the generated paths and unstages them. Tracked ones are marked
/// skip-worktree, and an untracked project.json is excluded. Linked worktrees
/// share `info/exclude` with their main checkout, so they get no exclude writes.
pub async fn apply(workspace: &Path) -> Result<()> {
    if !workspace.is_dir() {
        return Ok(());
    }
    for relative in GENERATED_PATHS {
        exclude_and_unstage(workspace, relative).await?;
        if tracked(workspace, relative).await? {
            git_output(
                workspace,
                &["update-index", "--skip-worktree", "--", relative],
            )
            .await?;
        }
    }
    if !tracked(workspace, PROJECT_JSON).await? {
        exclude_and_unstage(workspace, PROJECT_JSON).await?;
    }
    Ok(())
}

async fn exclude_and_unstage(workspace: &Path, relative: &str) -> Result<()> {
    exclude(workspace, relative).await?;
    // Unstaging a path git never indexed fails harmlessly; the Python oracle only logs it.
    let _ = git_succeeds(workspace, &["reset", "-q", "--", relative]).await;
    Ok(())
}

async fn tracked(workspace: &Path, relative: &str) -> Result<bool> {
    git_succeeds(workspace, &["ls-files", "--error-unmatch", "--", relative]).await
}

async fn exclude(workspace: &Path, pattern: &str) -> Result<()> {
    let Some(path) = info_exclude_path(workspace).await? else {
        return Ok(());
    };
    let existing = if path.exists() {
        fs::read_to_string(&path).with_context(|| format!("read {}", path.display()))?
    } else {
        String::new()
    };
    if existing.lines().any(|line| line.trim() == pattern) {
        return Ok(());
    }
    let mut text = existing;
    if !text.is_empty() && !text.ends_with('\n') {
        text.push('\n');
    }
    text.push_str(pattern);
    text.push('\n');
    write_atomic(&path, text.as_bytes())
}

/// The workspace's `info/exclude`, or `None` for a linked worktree.
async fn info_exclude_path(workspace: &Path) -> Result<Option<PathBuf>> {
    let git_dir = git_path(workspace, "--git-dir").await?;
    let common_dir = git_path(workspace, "--git-common-dir").await?;
    if fs::canonicalize(&git_dir)? != fs::canonicalize(&common_dir)? {
        return Ok(None);
    }
    let raw = git_output(workspace, &["rev-parse", "--git-path", "info/exclude"]).await?;
    Ok(Some(join_git_output(workspace, raw.trim())))
}

async fn git_path(workspace: &Path, arg: &str) -> Result<PathBuf> {
    let raw = git_output(workspace, &["rev-parse", arg]).await?;
    Ok(join_git_output(workspace, raw.trim()))
}

fn join_git_output(workspace: &Path, raw: &str) -> PathBuf {
    let path = Path::new(raw);
    if path.is_absolute() {
        path.to_owned()
    } else {
        workspace.join(path)
    }
}
