//! A panic still reaches stderr once routed logging is installed.

use std::path::PathBuf;
use std::process::Command;

use gobby_daemon::logging::{self, LogSettings};

/// Set in the child process to the logs directory it routes into.
const CHILD_LOGS: &str = "GOBBY_LOGGING_TEST_PANIC_CHILD_LOGS";
const PROBE: &str = "routed logging probe panic";

#[test]
fn panic_still_reaches_stderr_after_routing_installs() {
    if let Some(logs) = std::env::var_os(CHILD_LOGS) {
        // Child: install routing for the whole process, write one record, then panic.
        let activated = logging::activate(LogSettings::defaults(PathBuf::from(logs)));
        assert!(activated.is_ok(), "routing did not install: {activated:?}");
        tracing::info!(target: "gobby.daemon", "routing installed");
        panic!("{PROBE}");
    }

    let home = tempfile::tempdir().expect("temporary gobby home");
    let logs = home.path().join("logs");
    let output = Command::new(std::env::current_exe().expect("test binary path"))
        .args([
            "--exact",
            "logging::panic_still_reaches_stderr_after_routing_installs",
            "--nocapture",
        ])
        .env(CHILD_LOGS, &logs)
        .env("GOBBY_HOME", home.path())
        .output()
        .expect("child test runs");

    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains(PROBE), "{stderr}");
    assert!(
        !output.status.success(),
        "the child was meant to panic: {stderr}"
    );
    assert!(
        logs.join("daemon.log").exists(),
        "routing wrote no daemon.log under {}",
        logs.display()
    );
}
