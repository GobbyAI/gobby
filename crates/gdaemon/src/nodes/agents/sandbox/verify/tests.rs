use super::*;
use anyhow::{Context, Result};
use std::os::unix::fs::PermissionsExt;

fn node_script(dir: &Path, body: &str) -> Result<PathBuf> {
    let path = dir.join("node");
    fs::write(&path, format!("#!/bin/sh\n{body}\n"))?;
    fs::set_permissions(&path, fs::Permissions::from_mode(0o755))?;
    Ok(path)
}

#[test]
fn hung_node_probe_is_killed_at_the_deadline() -> Result<()> {
    let dir = tempfile::tempdir()?;
    // `exec` makes sleep the probe process itself, so the kill reaches the hang.
    let node = node_script(dir.path(), "exec sleep 30")?;
    let started = Instant::now();
    let error = check_node_version(&node, Duration::from_millis(200))
        .err()
        .context("a hung probe must be refused")?;
    assert!(error.to_string().contains("timed out"), "{error}");
    assert!(
        started.elapsed() < Duration::from_secs(5),
        "the probe ran past its deadline"
    );
    Ok(())
}

#[test]
fn nonzero_node_probe_is_refused_even_when_a_version_prints() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let node = node_script(dir.path(), "echo v22.12.0\nexit 1")?;
    let error = check_node_version(&node, Duration::from_secs(10))
        .err()
        .context("a failing probe must be refused")?;
    assert!(
        error.to_string().contains("could not be verified"),
        "{error}"
    );
    Ok(())
}

#[test]
fn current_node_version_passes_and_old_version_is_refused() -> Result<()> {
    let current_dir = tempfile::tempdir()?;
    let current = node_script(current_dir.path(), "echo v22.12.0")?;
    check_node_version(&current, Duration::from_secs(10))?;
    let old_dir = tempfile::tempdir()?;
    let old = node_script(old_dir.path(), "echo v18.0.0")?;
    let error = check_node_version(&old, Duration::from_secs(10))
        .err()
        .context("an old node must be refused")?;
    assert!(
        error.to_string().contains("or newer is required"),
        "{error}"
    );
    Ok(())
}
