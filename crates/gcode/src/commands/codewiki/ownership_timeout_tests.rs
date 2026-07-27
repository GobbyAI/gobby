use super::analysis::take_last_timed_out_git_blame_pid;
use super::*;
use std::process::Command;
use std::time::Duration;

use std::os::unix::fs::PermissionsExt;

#[test]
fn codewiki_ownership_timed_out_blame_reaps_child_before_returning() {
    let project = tempfile::tempdir().expect("project tempdir");
    let git = project.path().join("git");
    std::fs::write(&git, "#!/bin/sh\nexec sleep 5\n").expect("write fake git");
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
        let pid = take_last_timed_out_git_blame_pid()
            .expect("timeout path should capture the direct child pid")
            .to_string();
        assert!(
            !Command::new("kill")
                .args(["-0", &pid])
                .output()
                .expect("check fake git process")
                .status
                .success(),
            "timed-out blame process {pid} should be reaped before return"
        );
    }
}
