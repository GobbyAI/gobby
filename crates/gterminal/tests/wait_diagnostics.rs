//! #25733 criterion 2: a timed-out host-state wait names the host's pid, log tail
//! and probe state in its panic, not only the condition text.

use crate::host_support;

use std::time::Duration;

fn timeout_message(wait: impl FnOnce() + std::panic::UnwindSafe) -> String {
    let payload = std::panic::catch_unwind(wait).expect_err("the wait must time out");
    payload
        .downcast_ref::<String>()
        .cloned()
        .or_else(|| {
            payload
                .downcast_ref::<&str>()
                .map(|text| (*text).to_string())
        })
        .unwrap_or_default()
}

#[test]
fn host_wait_timeout_names_pid_log_and_probe_state() {
    let dir = tempfile::tempdir().expect("tempdir");
    std::fs::write(dir.path().join(host_support::PID_FILE), "4242").expect("pid file");
    std::fs::write(dir.path().join(host_support::SPAWN_PID_FILE), "4243").expect("spawn file");
    std::fs::write(dir.path().join("gterm.log"), "first line\nlast line\n").expect("log");
    std::fs::write(dir.path().join("probe.started"), "").expect("probe marker");

    let path = dir.path().to_path_buf();
    let text = timeout_message(move || {
        host_support::wait_until_host_within(
            &path,
            "the probe holds its FIFO",
            Duration::from_millis(50),
            || false,
        );
    });

    assert!(text.contains("the probe holds its FIFO"), "{text}");
    assert!(text.contains("pid=\"4242\""), "{text}");
    assert!(text.contains("spawned_pid=\"4243\""), "{text}");
    assert!(text.contains("host_state="), "{text}");
    assert!(text.contains("last line"), "{text}");
    assert!(text.contains("probe_markers=[\"probe.started\"]"), "{text}");
}
