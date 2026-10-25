//! Seeds an isolated uv project environment from the host uv cache, offline.

use anyhow::{Context, Result, ensure};
use std::{
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};
use tokio::process::Command;

/// uv variables the seed child must not inherit; `--cache-dir` names the cache instead.
const CLEARED_UV_ENV: [&str; 4] = [
    "UV_CACHE_DIR",
    "UV_PROJECT_ENVIRONMENT",
    "VIRTUAL_ENV",
    "VIRTUAL_ENV_PROMPT",
];
const CACHE_DIR_TIMEOUT: Duration = Duration::from_secs(10);

/// Runs `uv sync --offline --frozen` in `workspace` against the host cache.
/// Skips when the project files, `uv`, or the cache directory are missing.
/// A failed or timed-out sync is returned as an error for the caller to warn on.
pub async fn preseed(workspace: &Path, uv: Option<&Path>, timeout: Duration) -> Result<()> {
    if !workspace.is_dir()
        || !workspace.join("pyproject.toml").is_file()
        || !workspace.join("uv.lock").is_file()
    {
        return Ok(());
    }
    let Some(uv) = uv else {
        return Ok(());
    };
    let cache = cache_dir(uv, workspace).await;
    if !cache.is_dir() {
        return Ok(());
    }

    let mut command = seed_command(uv, workspace);
    command
        .args([
            "sync",
            "--offline",
            "--frozen",
            "--no-progress",
            "--link-mode",
            "copy",
        ])
        .arg("--cache-dir")
        .arg(&cache)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    // Dropping the child on timeout kills it; piped output would hold the wait open.
    let status = tokio::time::timeout(timeout, command.status())
        .await
        .context("uv sync timed out")?
        .context("start uv sync")?;
    ensure!(status.success(), "uv sync exited with {status}");
    Ok(())
}

/// Resolves uv's default cache directory, falling back to `~/.cache/uv`.
async fn cache_dir(uv: &Path, workspace: &Path) -> PathBuf {
    let mut command = seed_command(uv, workspace);
    command.args(["cache", "dir"]);
    if let Ok(Ok(output)) = tokio::time::timeout(CACHE_DIR_TIMEOUT, command.output()).await
        && output.status.success()
    {
        let text = String::from_utf8_lossy(&output.stdout).trim().to_owned();
        if !text.is_empty() {
            return PathBuf::from(text);
        }
    }
    std::env::var_os("HOME")
        .map(|home| PathBuf::from(home).join(".cache/uv"))
        .unwrap_or_default()
}

fn seed_command(uv: &Path, workspace: &Path) -> Command {
    let mut command = Command::new(uv);
    command
        .current_dir(workspace)
        .env("UV_PYTHON_DOWNLOADS", "never")
        .kill_on_drop(true);
    for variable in CLEARED_UV_ENV {
        command.env_remove(variable);
    }
    command
}
