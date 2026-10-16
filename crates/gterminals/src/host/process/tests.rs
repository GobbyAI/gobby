use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

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
async fn gterm_stays_the_host_after_its_binary_is_replaced_and_unlinked() {
    let dir = TempDir::new().unwrap();
    let binary = named_gterm(dir.path(), "/bin/sleep");
    let mut child = Command::new(&binary).arg("600").spawn().unwrap();
    // Promotion renames a new inode over the name; then the name goes away too.
    let staged = dir.path().join(".gterm.new");
    std::fs::copy("/bin/sleep", &staged).unwrap();
    std::fs::rename(&staged, &binary).unwrap();
    std::fs::remove_file(&binary).unwrap();
    let observed = process_is_host(child.id()).await;
    child.kill().unwrap();
    child.wait().unwrap();
    assert!(
        observed.unwrap(),
        "a live gterm whose executable was replaced is still the host"
    );
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
    // Wait for the exit but leave the child unreaped, so it stays a zombie. The
    // wait is bounded: an unbounded waitid once hung a run past 480s.
    let pid = Pid::from_child(&child);
    let (exited, exit) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let status = waitid(
            WaitId::Pid(pid),
            WaitIdOptions::EXITED | WaitIdOptions::NOWAIT,
        )
        .map(|status| status.is_some());
        // The receiver is gone only after a timeout, which already failed the test.
        let _ = exited.send(status);
    });
    match exit.recv_timeout(Duration::from_secs(10)) {
        Ok(status) => assert!(status.unwrap(), "waitid reported no exited child"),
        Err(_) => {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("gterm fixture did not exit within 10s");
        }
    }
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
