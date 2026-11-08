use super::*;

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
