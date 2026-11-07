use super::{BackendChild, BackendConfig, lock_reporting};
use std::{sync::Mutex as StdMutex, time::Duration};
use tokio::{sync::Mutex, time::timeout};

/// A lease lock held by another task is reported each interval while it stays held, and taking
/// it succeeds once the holder releases it.
#[tokio::test]
async fn lock_reporting_reports_each_interval_until_the_lock_is_released() {
    let lock = Mutex::new(7u8);
    let held = lock.lock().await;
    let lines = StdMutex::new(Vec::<String>::new());
    let reporter = |line: &str| lines.lock().expect("report sink").push(line.to_owned());

    let waited = timeout(
        Duration::from_millis(250),
        lock_reporting(&lock, Duration::from_millis(20), &reporter),
    )
    .await;
    assert!(
        waited.is_err(),
        "the wait ends only when the lock is released"
    );
    let reports = lines.lock().expect("report sink").clone();
    assert!(
        reports.len() >= 3,
        "expected a report each interval: {reports:?}"
    );
    assert!(
        reports.iter().all(|line| line.contains("lease lock")),
        "{reports:?}"
    );

    drop(held);
    let guard = lock_reporting(&lock, Duration::from_millis(20), &reporter).await;
    assert_eq!(*guard, 7);
}

/// Darwin reports EPERM for a group whose members have all exited, and `waitpid` reports an
/// exited leader only after that. Signalling a group that has just finished must report it as
/// done, not as a permission error.
#[cfg(target_os = "macos")]
#[tokio::test]
async fn group_signal_after_leader_exit_is_not_a_permission_error() -> anyhow::Result<()> {
    let home = tempfile::tempdir()?;
    let config =
        BackendConfig::from_command_json(home.path(), Some(r#"["/bin/sh","-c","exit 0"]"#))?;
    for trial in 0..200 {
        let mut backend = BackendChild::spawn(&config)?;
        let mut reaped = false;
        for _ in 0..10_000 {
            // drain and kill_group signal the group until the leader is reaped.
            let signalled = backend.signal_owned_group(libc::SIGKILL);
            assert!(
                signalled.is_ok(),
                "trial {trial}: signalling a group whose leader exited: {signalled:?}"
            );
            if backend.child.try_wait()?.is_some() {
                reaped = true;
                break;
            }
            tokio::task::yield_now().await;
        }
        assert!(reaped, "trial {trial}: the leader was never reaped");
    }
    Ok(())
}
