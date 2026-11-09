use super::*;
use super::{BackendChild, BackendConfig, lock_reporting};
use crate::lease::NamedLock;
use std::{sync::Mutex as StdMutex, time::Duration};
use tokio::time::timeout;

/// A lease lock held by another task is reported each interval while it stays held, and taking
/// it succeeds once the holder releases it.
#[tokio::test]
async fn lock_reporting_reports_each_interval_until_the_lock_is_released() {
    let lock = NamedLock::new(7u8);
    let held = lock.lock_as("test holder").await;
    let lines = StdMutex::new(Vec::<String>::new());
    let reporter = |line: &str| lines.lock().expect("report sink").push(line.to_owned());

    let waited = timeout(
        Duration::from_millis(250),
        lock_reporting(
            &lock,
            Duration::from_millis(20),
            Duration::from_secs(60),
            &reporter,
        ),
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
        reports
            .iter()
            .all(|line| line.contains("lease lock") && line.contains("test holder")),
        "every report must name the lock holder: {reports:?}"
    );

    drop(held);
    let guard = lock_reporting(
        &lock,
        Duration::from_millis(20),
        Duration::from_secs(60),
        &reporter,
    )
    .await
    .expect("the lock is free once released");
    assert_eq!(*guard, 7);
}

/// A lease lock still held past the supervisor's bound fails the wait, and the error names the
/// holder. The wait does not go on silently.
#[tokio::test]
async fn lock_reporting_gives_up_at_its_bound_and_names_the_holder() {
    let lock = NamedLock::new(7u8);
    let _held = lock.lock_as("test holder").await;
    let reporter = |_: &str| {};

    let outcome = timeout(
        Duration::from_secs(5),
        lock_reporting(
            &lock,
            Duration::from_millis(20),
            Duration::from_millis(200),
            &reporter,
        ),
    )
    .await;
    let error = match outcome {
        Ok(Err(error)) => error,
        Ok(Ok(_)) => panic!("the lock is held, so the wait cannot succeed"),
        Err(_) => panic!("the wait did not end within its bound"),
    };
    assert!(
        format!("{error:#}").contains("test holder"),
        "the error must name the holder: {error:#}"
    );
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
        let mut backend = BackendChild::spawn(&config).await?;
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

/// How long the forked child stalls before exec. Its exec-status pipe stays open the whole time.
const STALL: Duration = Duration::from_secs(5);

/// A backend command whose forked child stalls in `pre_exec` before it execs.
fn stalled_command() -> Command {
    let mut command = Command::new("/usr/bin/true");
    command.kill_on_drop(true);
    // SAFETY: the closure only sleeps in the forked child before exec. Sleeping is
    // async-signal-safe there, and the closure touches no shared state.
    unsafe {
        command.pre_exec(|| {
            std::thread::sleep(STALL);
            Ok(())
        });
    }
    command
}

/// Reproduction without the fix: `Command::spawn` returns only after the child execs, so a child
/// stalled before exec blocks the caller for the whole stall.
#[tokio::test]
async fn plain_spawn_blocks_for_a_child_stalled_before_exec() {
    let started = std::time::Instant::now();
    let _child = stalled_command()
        .spawn()
        .expect("the stalled child execs after the stall");
    let waited = started.elapsed();
    assert!(
        waited >= STALL,
        "spawn returned after {waited:?}, before the stall ended"
    );
}

/// Passing rerun with the fix: the bounded spawn returns at its deadline while the child is still
/// stalled, and the error names the deadline.
#[tokio::test]
async fn bounded_spawn_returns_at_its_deadline_for_a_stalled_child() {
    let started = std::time::Instant::now();
    let error = spawn_bounded(stalled_command(), Duration::from_millis(200))
        .await
        .expect_err("a stalled child must not hold the spawn past its deadline");
    let waited = started.elapsed();
    assert!(waited < STALL, "bounded spawn blocked for {waited:?}");
    assert!(error.to_string().contains("did not exec"), "{error:#}");
}

/// The runtime keeps running while a spawn waits on a stalled child. The clock is paused, so the
/// 50 ms timer is the first event and the spawn must still be pending when it fires. A blocking
/// wait finishes the spawn first, when the real stall ends, and fails the select. Virtual time
/// makes the ordering independent of host load.
#[tokio::test(start_paused = true)]
async fn bounded_spawn_leaves_the_runtime_free_while_a_child_is_stalled() {
    let spawn = spawn_bounded(stalled_command(), Duration::from_millis(500));
    tokio::pin!(spawn);
    tokio::select! {
        biased;
        _ = &mut spawn => {
            panic!("spawn finished before the timer; the stalled child blocked the runtime")
        }
        () = tokio::time::sleep(Duration::from_millis(50)) => {}
    }
    assert!(
        spawn.await.is_err(),
        "a stalled child must miss the deadline"
    );
}
