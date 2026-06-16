use super::*;
use std::time::Duration;

use std::os::unix::fs::PermissionsExt;
#[cfg(target_os = "macos")]
use std::process::Command;

#[test]
fn codewiki_ownership_timed_out_blame_reaps_child_without_thread_leak() {
    let Some(baseline_threads) = current_thread_count() else {
        return;
    };
    let project = tempfile::tempdir().expect("project tempdir");
    let git = project.path().join("git");
    std::fs::write(&git, "#!/bin/sh\nsleep 5\n").expect("write fake git");
    let mut permissions = std::fs::metadata(&git)
        .expect("fake git metadata")
        .permissions();
    permissions.set_mode(0o755);
    std::fs::set_permissions(&git, permissions).expect("mark fake git executable");

    for _ in 0..3 {
        let output = git_blame_output_with_timeout(
            &git,
            project.path(),
            "HEAD",
            "src/lib.rs",
            Duration::from_millis(10),
        )
        .expect("timed git blame");
        assert!(output.is_none());
    }
    let Some(after_threads) = current_thread_count() else {
        return;
    };
    // The Rust test harness can start an unrelated worker while this
    // process-wide count is sampled. Repeated timed-out blames must not add
    // one thread per timeout.
    assert!(
        after_threads <= baseline_threads + 1,
        "timed-out blame should not leak threads: before={baseline_threads}, after={after_threads}"
    );
}

#[cfg(target_os = "linux")]
fn current_thread_count() -> Option<usize> {
    std::fs::read_to_string("/proc/self/status")
        .ok()?
        .lines()
        .find_map(|line| {
            line.strip_prefix("Threads:")
                .and_then(|value| value.trim().parse().ok())
        })
}

#[cfg(target_os = "macos")]
fn current_thread_count() -> Option<usize> {
    let output = Command::new("ps")
        .args(["-M", "-p", &std::process::id().to_string()])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter(|line| !line.trim().is_empty())
        .count()
        .checked_sub(1)
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
fn current_thread_count() -> Option<usize> {
    None
}
