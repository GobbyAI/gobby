//! Local checkout preparation; project registration stays daemon-owned.

use std::ffi::OsStr;
use std::path::{Path, PathBuf};
use std::time::Duration;

use tokio::fs;
use tokio::process::Command;

use super::projects::daemon_reason;
use crate::daemon::{Daemon, ProjectRow};
use crate::ui::dialogs::project::expand_home;

pub(super) async fn resolve_project(
    daemon: &impl Daemon,
    path: &str,
    home: &Path,
    create: bool,
) -> Result<ProjectRow, String> {
    if path.trim().is_empty() {
        return Err("Enter a project directory path.".into());
    }
    let expanded = PathBuf::from(expand_home(path.trim(), home));
    let root = if create {
        new_checkout(&expanded, OsStr::new("git")).await?
    } else {
        checkout_root(&expanded).await?
    };
    if !create {
        let projects = daemon.projects().await.map_err(|e| daemon_reason(&e))?;
        for project in projects {
            if let Some(checkout) = &project.checkout {
                if fs::canonicalize(&checkout.root_path).await.ok().as_ref() == Some(&root) {
                    return Ok(project);
                }
            }
        }
    }
    let path = root.to_str().ok_or("Project path must be valid UTF-8.")?;
    daemon.init_project(path).await.map_err(|error| {
        let error = daemon_reason(&error);
        if create {
            format!("Registration failed: {error}. Checkout kept; retry with Open project.")
        } else {
            format!("Registration failed: {error}")
        }
    })
}

async fn new_checkout(path: &Path, git: &OsStr) -> Result<PathBuf, String> {
    // create_dir is exclusive; never remove a path, even when this attempt created it.
    match fs::create_dir(path).await {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            let mut entries = fs::read_dir(path)
                .await
                .map_err(|e| format!("Cannot use directory: {e}"))?;
            if entries
                .next_entry()
                .await
                .map_err(|e| e.to_string())?
                .is_some()
            {
                return Err(
                    "Directory is not empty. Choose a new or empty directory, or use Open project."
                        .into(),
                );
            }
        }
        Err(error) => return Err(format!("Cannot create directory: {error}")),
    }
    let root = fs::canonicalize(path).await.map_err(|e| e.to_string())?;
    git_output(git, &root, &["init"]).await.map_err(|error| {
        format!("Git initialization failed: {error}. Directory kept; inspect it before retrying.")
    })?;
    Ok(root)
}

async fn checkout_root(path: &Path) -> Result<PathBuf, String> {
    let path = fs::canonicalize(path)
        .await
        .map_err(|e| format!("Cannot open directory: {e}"))?;
    let inside = git_output(
        OsStr::new("git"),
        &path,
        &["rev-parse", "--is-inside-work-tree"],
    )
    .await
    .map_err(|error| {
        format!("Cannot verify git checkout: {error}. Choose an existing checkout.")
    })?;
    if inside.trim() != "true" {
        return Err("Path is not a git checkout. Choose an existing checkout.".into());
    }
    let root = git_output(OsStr::new("git"), &path, &["rev-parse", "--show-toplevel"]).await?;
    fs::canonicalize(root.strip_suffix('\n').unwrap_or(&root))
        .await
        .map_err(|e| e.to_string())
}

async fn git_output(git: &OsStr, path: &Path, args: &[&str]) -> Result<String, String> {
    let output = tokio::time::timeout(
        Duration::from_secs(10),
        Command::new(git)
            .arg("-C")
            .arg(path)
            .args(args)
            .env_remove("GIT_DIR")
            .env_remove("GIT_WORK_TREE")
            .env_remove("GIT_COMMON_DIR")
            .kill_on_drop(true)
            .output(),
    )
    .await
    .map_err(|_| "git timed out".to_string())?
    .map_err(|e| e.to_string())?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_owned());
    }
    String::from_utf8(output.stdout).map_err(|_| "Git checkout path must be valid UTF-8.".into())
}

#[cfg(test)]
#[path = "project_paths/tests.rs"]
mod tests;
