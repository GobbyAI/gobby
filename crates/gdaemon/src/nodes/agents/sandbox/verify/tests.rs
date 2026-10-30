use super::*;
use anyhow::{Context, Result};
use std::os::unix::fs::PermissionsExt;

#[test]
fn srt_content_pins_match_python_and_bundled_runner() {
    let source = include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../src/gobby/utils/dependency_requirements.py"
    ));
    for (name, digest) in [
        ("tarball_sha256", SRT_RELEASE.tarball_sha256),
        ("lockfile_sha256", SRT_RELEASE.lockfile_sha256),
        ("runner_sha256", SRT_RELEASE.runner_sha256),
        ("http_proxy_sha256", SRT_RELEASE.http_proxy_sha256),
        ("mux_proxy_sha256", SRT_RELEASE.mux_proxy_sha256),
        ("macos_profile_sha256", SRT_RELEASE.macos_profile_sha256),
        ("which_sha256", SRT_RELEASE.which_sha256),
    ] {
        assert!(
            source.contains(&format!("{name}=\"{digest}\"")),
            "{name} differs from Python SRT pin"
        );
    }
    assert_eq!(sha256_hex(BUNDLED_RUNNER), SRT_RELEASE.runner_sha256);
}

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
fn inherited_stdout_cannot_outlive_the_node_probe_deadline() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let pidfile = dir.path().join("descendant.pid");
    let node = node_script(
        dir.path(),
        &format!(
            "(sleep 30) &\necho $! > '{}'\necho v22.12.0\nexit 0",
            pidfile.display()
        ),
    )?;
    let started = Instant::now();
    let result = check_node_version(&node, Duration::from_millis(200));
    let elapsed = started.elapsed();
    let pid: i32 = fs::read_to_string(pidfile)?.trim().parse()?;
    // SAFETY: this is the isolated sleeper created by our fake node, never a user process.
    unsafe { libc::kill(pid, libc::SIGKILL) };
    assert!(
        elapsed < Duration::from_secs(2),
        "stdout outlived the deadline: {elapsed:?}"
    );
    let error = result.err().context("inherited stdout must time out")?;
    assert!(error.to_string().contains("timed out"), "{error}");
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
