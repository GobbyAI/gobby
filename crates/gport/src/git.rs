//! Git reads. Every call names its directory, so tests run against a temp repository.

use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{Context, bail};

/// Run git in `dir` and return its stdout. A non-zero exit is an error carrying stderr.
pub fn output(dir: &Path, args: &[&str]) -> anyhow::Result<Vec<u8>> {
    let result = Command::new("git")
        .current_dir(dir)
        .args(args)
        .output()
        .with_context(|| format!("cannot run git {}", args.join(" ")))?;
    if !result.status.success() {
        bail!(
            "git {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&result.stderr).trim()
        );
    }
    Ok(result.stdout)
}

/// Run git in `dir` and return its stdout as trimmed text.
pub fn text(dir: &Path, args: &[&str]) -> anyhow::Result<String> {
    Ok(String::from_utf8(output(dir, args)?)
        .context("git printed text that is not UTF-8")?
        .trim()
        .to_owned())
}

/// Whether git in `dir` exits 0 for `args`. Used for `merge-base --is-ancestor`.
pub fn succeeds(dir: &Path, args: &[&str]) -> anyhow::Result<bool> {
    Ok(Command::new("git")
        .current_dir(dir)
        .args(args)
        .output()
        .with_context(|| format!("cannot run git {}", args.join(" ")))?
        .status
        .success())
}

/// The bytes of `path` at `revision`, or `None` when that revision has no such path.
pub fn show(dir: &Path, revision: &str, path: &str) -> anyhow::Result<Option<Vec<u8>>> {
    let object = format!("{revision}:{path}");
    if !succeeds(dir, &["cat-file", "-e", &object])? {
        return Ok(None);
    }
    output(dir, &["show", &object]).map(Some)
}

/// The top level of the checkout that holds `dir`.
pub fn top_level(dir: &Path) -> anyhow::Result<PathBuf> {
    Ok(PathBuf::from(text(dir, &["rev-parse", "--show-toplevel"])?))
}
