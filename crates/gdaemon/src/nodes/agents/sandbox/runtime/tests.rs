use super::*;
use anyhow::{Context, Result};
use std::os::unix::fs::PermissionsExt;

fn runner_script(dir: &Path, body: &str) -> Result<Vec<String>> {
    let path = dir.join("runner");
    fs::write(&path, format!("#!/bin/sh\n{body}\n"))?;
    fs::set_permissions(&path, fs::Permissions::from_mode(0o755))?;
    Ok(vec![path.display().to_string(), "--preflight".to_string()])
}

#[test]
fn inherited_preflight_stderr_cannot_outlive_the_deadline() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let pidfile = dir.path().join("descendant.pid");
    let command = runner_script(
        dir.path(),
        &format!(
            "(sleep 30) &\necho $! > '{}'\necho preflight-detail >&2\nexit 1",
            pidfile.display()
        ),
    )?;
    let started = Instant::now();
    let result = run_preflight(
        &command,
        dir.path(),
        &BTreeMap::new(),
        Duration::from_secs(1),
    );
    let elapsed = started.elapsed();
    let pid: i32 = fs::read_to_string(&pidfile)?.trim().parse()?;
    // SAFETY: this is the isolated sleeper created by our fake runner, never a user process.
    unsafe { libc::kill(pid, libc::SIGKILL) };
    assert!(
        elapsed < Duration::from_secs(5),
        "stderr outlived the preflight: {elapsed:?}"
    );
    let error = result.err().context("a failed runner must be refused")?;
    assert!(error.to_string().contains("preflight-detail"), "{error}");
    Ok(())
}
