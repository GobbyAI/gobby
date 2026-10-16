use std::path::{Path, PathBuf};
use std::process::Command;

use rustix::process::{Pid, WaitId, WaitIdOptions, waitid};
use tempfile::TempDir;

use super::process_is_host;

/// Copies `source` to `<dir>/gterm`, signed like the host_lifecycle fixture.
fn named_gterm(dir: &Path, source: &str) -> PathBuf {
    let binary = dir.join("gterm");
    std::fs::copy(source, &binary).unwrap();
    #[cfg(target_os = "macos")]
    assert!(
        Command::new("/usr/bin/codesign")
            .args(["--force", "--sign", "-"])
            .arg(&binary)
            .output()
            .unwrap()
            .status
            .success(),
        "gterm fixture executable must be signed before launch"
    );
    binary
}

#[tokio::test]
async fn live_gterm_is_the_host() {
    let dir = TempDir::new().unwrap();
    let mut child = Command::new(named_gterm(dir.path(), "/bin/sleep"))
        .arg("600")
        .spawn()
        .unwrap();
    let observed = process_is_host(child.id()).await;
    child.kill().unwrap();
    child.wait().unwrap();
    assert!(observed.unwrap(), "a live gterm process is the host");
}

#[tokio::test]
async fn reaped_gterm_is_not_the_host() {
    let dir = TempDir::new().unwrap();
    let mut child = Command::new(named_gterm(dir.path(), "/usr/bin/true"))
        .spawn()
        .unwrap();
    let pid = child.id();
    child.wait().unwrap();
    assert!(
        !process_is_host(pid).await.unwrap(),
        "a dead pid is not the host"
    );
}

#[tokio::test]
async fn zombie_gterm_is_not_the_host() {
    let dir = TempDir::new().unwrap();
    let mut child = Command::new(named_gterm(dir.path(), "/usr/bin/true"))
        .spawn()
        .unwrap();
    // Block until the child exits but leave it unreaped, so it stays a zombie.
    waitid(
        WaitId::Pid(Pid::from_child(&child)),
        WaitIdOptions::EXITED | WaitIdOptions::NOWAIT,
    )
    .unwrap();
    let observed = process_is_host(child.id()).await;
    child.wait().unwrap();
    assert!(!observed.unwrap(), "a zombie gterm is not the host");
}

#[tokio::test]
async fn live_process_with_another_name_is_not_the_host() {
    assert!(
        !process_is_host(std::process::id()).await.unwrap(),
        "the test binary is not named gterm"
    );
}
