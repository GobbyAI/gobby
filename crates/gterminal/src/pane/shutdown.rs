//! Process teardown for an owned pane session.

use std::sync::atomic::{AtomicBool, Ordering};

use crate::layout::PaneId;
use tracing::{info, warn};

pub(crate) fn process_alive_for_shutdown(
    pid: u32,
    child_pid: u32,
    child_wait_completed: bool,
    process_exists: impl FnOnce(u32) -> bool,
) -> bool {
    if pid == child_pid && child_wait_completed {
        return false;
    }
    process_exists(pid)
}

pub(crate) fn wait_for_processes_to_exit(
    pids: &[u32],
    child_pid: u32,
    child_wait_completed: Option<&AtomicBool>,
    timeout: std::time::Duration,
) -> bool {
    let deadline = std::time::Instant::now() + timeout;
    loop {
        let completed = child_wait_completed
            .map(|flag| flag.load(Ordering::Acquire))
            .unwrap_or(false);
        let any_alive = pids.iter().any(|pid| {
            process_alive_for_shutdown(*pid, child_pid, completed, crate::platform::process_exists)
        });
        if !any_alive {
            return true;
        }
        if std::time::Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
}

pub(crate) fn shutdown_pane_processes(
    pane_id: PaneId,
    child_pid: u32,
    child_wait_completed: Option<&AtomicBool>,
) {
    if child_pid == 0 {
        return;
    }

    let mut pids = crate::platform::session_processes(child_pid);
    if pids.is_empty() {
        pids.push(child_pid);
    }
    pids.sort_unstable();
    pids.dedup();

    for (signal, grace) in [
        (
            crate::platform::Signal::Hangup,
            std::time::Duration::from_millis(250),
        ),
        (
            crate::platform::Signal::Terminate,
            std::time::Duration::from_millis(250),
        ),
        (
            crate::platform::Signal::Kill,
            std::time::Duration::from_millis(250),
        ),
    ] {
        crate::platform::signal_processes(&pids, signal);
        if wait_for_processes_to_exit(&pids, child_pid, child_wait_completed, grace) {
            info!(
                pane = pane_id.raw(),
                pid = child_pid,
                ?signal,
                "pane session terminated"
            );
            return;
        }
    }

    warn!(
        pane = pane_id.raw(),
        pid = child_pid,
        pids = ?pids,
        "pane session still alive after forced shutdown"
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shutdown_liveness_treats_reaped_direct_child_as_gone() {
        assert!(!process_alive_for_shutdown(7, 7, true, |_| true));
    }

    #[test]
    fn shutdown_liveness_keeps_unreaped_direct_child_alive() {
        assert!(process_alive_for_shutdown(7, 7, false, |_| true));
    }

    #[test]
    fn shutdown_liveness_keeps_other_session_processes_alive() {
        assert!(process_alive_for_shutdown(9, 7, true, |_| true));
    }

    #[test]
    fn shutdown_liveness_treats_missing_process_as_gone() {
        assert!(!process_alive_for_shutdown(7, 7, false, |_| false));
    }
}
