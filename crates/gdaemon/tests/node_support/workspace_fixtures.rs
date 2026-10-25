//! Shared fixtures for the node workspace lifecycle tests.

use anyhow::{Result, ensure};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::Command,
};

pub const PROJECT_ID: &str = "project-1";

/// Fixed home and hooks paths. They appear verbatim in the droid golden and
/// are never created on disk.
pub const FIXTURE_HOME: &str = "/gobby-fixture/home";
pub const FIXTURE_HOOKS_DIR: &str = "/gobby-fixture/home/hooks";

pub fn golden_path(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/node_isolation")
        .join(name)
}

pub fn git(dir: &Path, args: &[&str]) -> Result<String> {
    let output = Command::new("git").arg("-C").arg(dir).args(args).output()?;
    ensure!(
        output.status.success(),
        "git {args:?} failed in {}: {}",
        dir.display(),
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(String::from_utf8(output.stdout)?)
}

/// A checkout on `main` with one commit. Its untracked `.gobby/project.json`
/// is the parent identity that repair copies into each workspace.
pub fn source_repo(root: &Path) -> Result<PathBuf> {
    let source = root.join("source");
    fs::create_dir_all(&source)?;
    git(&source, &["init", "-q", "-b", "main"])?;
    git(
        &source,
        &["config", "user.email", "fixture@example.invalid"],
    )?;
    git(&source, &["config", "user.name", "Fixture"])?;
    git(&source, &["config", "commit.gpgsign", "false"])?;
    fs::write(source.join("README.md"), "fixture\n")?;
    git(&source, &["add", "README.md"])?;
    git(&source, &["commit", "-q", "-m", "init"])?;
    fs::create_dir_all(source.join(".gobby"))?;
    fs::write(
        source.join(".gobby/project.json"),
        format!("{{\"id\": \"{PROJECT_ID}\", \"name\": \"fixture\"}}\n"),
    )?;
    Ok(source)
}

/// Clones `source` to `dest`, the starting shape of a clone-mode workspace.
pub fn clone_of(source: &Path, dest: &Path) -> Result<PathBuf> {
    if let Some(parent) = dest.parent() {
        fs::create_dir_all(parent)?;
    }
    let output = Command::new("git")
        .args(["clone", "-q"])
        .arg(source)
        .arg(dest)
        .output()?;
    ensure!(
        output.status.success(),
        "git clone into {} failed: {}",
        dest.display(),
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(dest.to_owned())
}

/// Whether git would ignore `relative` in `workspace`.
pub fn is_git_ignored(workspace: &Path, relative: &str) -> bool {
    Command::new("git")
        .arg("-C")
        .arg(workspace)
        .args(["check-ignore", "-q", relative])
        .status()
        .is_ok_and(|status| status.success())
}

/// Writes a `uv` stand-in into `dir`. Each call appends one `argv:` block to
/// the returned log: the argv, then the seed-relevant env vars as the child
/// sees them. `cache dir` prints `cache_dir`; any other call runs `sync_body`.
pub fn fake_uv(dir: &Path, cache_dir: &Path, sync_body: &str) -> Result<(PathBuf, PathBuf)> {
    fs::create_dir_all(dir)?;
    let bin = dir.join("uv");
    let log = dir.join("uv.log");
    let script = r#"#!/bin/sh
{
  printf 'argv:'
  for a in "$@"; do printf ' [%s]' "$a"; done
  printf '\n'
  printf 'UV_PYTHON_DOWNLOADS=%s\n' "${UV_PYTHON_DOWNLOADS-unset}"
  printf 'UV_CACHE_DIR=%s\n' "${UV_CACHE_DIR-unset}"
  printf 'UV_PROJECT_ENVIRONMENT=%s\n' "${UV_PROJECT_ENVIRONMENT-unset}"
  printf 'VIRTUAL_ENV=%s\n' "${VIRTUAL_ENV-unset}"
  printf 'VIRTUAL_ENV_PROMPT=%s\n' "${VIRTUAL_ENV_PROMPT-unset}"
} >> '@LOG@'
if [ "$1 $2" = "cache dir" ]; then
  printf '%s\n' '@CACHE@'
  exit 0
fi
@SYNC@
"#
    .replace("@LOG@", &log.display().to_string())
    .replace("@CACHE@", &cache_dir.display().to_string())
    .replace("@SYNC@", sync_body);
    fs::write(&bin, script)?;
    fs::set_permissions(&bin, fs::Permissions::from_mode(0o755))?;
    Ok((bin, log))
}
