use super::*;
use crate::lease::NamedLock;
use std::{sync::Mutex as StdMutex, time::Duration};
use tokio::time::timeout;

#[tokio::test]
async fn backend_exec_seals_inherited_descriptors() -> anyhow::Result<()> {
    use std::os::fd::{FromRawFd, OwnedFd};

    let home = tempfile::tempdir()?;
    let source = std::fs::File::open("/dev/null")?;
    // SAFETY: F_DUPFD duplicates a live owned descriptor. The successful result
    // is uniquely owned below; unlike F_DUPFD_CLOEXEC it deliberately survives exec.
    let raw = unsafe { libc::fcntl(source.as_raw_fd(), libc::F_DUPFD, 512) };
    if raw < 0 {
        return Err(io::Error::last_os_error().into());
    }
    // SAFETY: the successful duplication returned a new, unowned descriptor.
    let inherited = unsafe { OwnedFd::from_raw_fd(raw) };
    let program = r#"import errno,os,sys
try:
    os.fstat(int(sys.argv[1]))
except OSError as error:
    assert error.errno == errno.EBADF
else:
    raise AssertionError('inherited descriptor survived backend exec')
assert os.read(int(os.environ['GOBBY_SUPERVISOR_FD']),4096).endswith(b'\n')
"#;
    let command = serde_json::to_string(&[
        "uv",
        "run",
        "--no-sync",
        "python",
        "-c",
        program,
        &raw.to_string(),
    ])?;
    let config = BackendConfig::from_command_json(home.path(), Some(&command))?;
    let mut backend = BackendChild::spawn(&config).await?;
    let status = timeout(Duration::from_secs(10), backend.child.wait()).await??;
    backend.finish_stderr().await;
    assert!(
        status.success(),
        "backend stderr: {}",
        backend.last_line.borrow().as_str()
    );
    drop(inherited);
    Ok(())
}

#[tokio::test]
async fn sealed_backend_exec_still_reports_spawn_errors() -> anyhow::Result<()> {
    let home = tempfile::tempdir()?;
    let command = serde_json::to_string(&[home.path().join("absent-executable")])?;
    let config = BackendConfig::from_command_json(home.path(), Some(&command))?;
    let result = BackendChild::spawn(&config).await;
    let error = match result {
        Ok(_) => anyhow::bail!("missing executable unexpectedly spawned"),
        Err(error) => error,
    };
    assert_eq!(
        error
            .root_cause()
            .downcast_ref::<io::Error>()
            .map(io::Error::kind),
        Some(io::ErrorKind::NotFound)
    );
    Ok(())
}

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
    let error = spawn_bounded(
        Vec::new(),
        || stalled_command().spawn(),
        Duration::from_millis(200),
    )
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
    let spawn = spawn_bounded(
        Vec::new(),
        || stalled_command().spawn(),
        Duration::from_millis(500),
    );
    tokio::pin!(spawn);
    assert!(
        timeout(Duration::from_millis(50), &mut spawn)
            .await
            .is_err(),
        "spawn finished before the timer; the stalled child blocked the runtime"
    );
    assert!(
        spawn.await.is_err(),
        "a stalled child must miss the deadline"
    );
}

/// The descriptor hazard. `BackendChild::spawn` hands the liveness read end to `spawn_bounded`, and
/// a `pre_exec` clears CLOEXEC on its raw number. The caller gives up while the spawn is stalled
/// before fork. If the caller had released the read end, an unrelated descriptor would take its
/// number, and the child would inherit it. The helper must keep the number occupied until spawn
/// returns, so the child finds the liveness pipe there.
#[tokio::test]
async fn bounded_spawn_never_hands_the_child_a_descriptor_reused_after_the_caller_gave_up() {
    use std::io::Read;
    use std::os::fd::AsRawFd;

    let (reader, _writer) = std::io::pipe().expect("liveness pipe");
    let read_fd = reader.as_raw_fd();
    let (mut done, done_writer) = std::io::pipe().expect("completion pipe");
    let (release, gate) = std::sync::mpsc::channel::<()>();
    // The child reports `unexpected` when its liveness number is not a pipe, then `done`.
    let script = format!("test -p /dev/fd/{read_fd} || echo unexpected >&2; echo done >&2");
    let mut command = Command::new("/bin/sh");
    // Stdout is discarded; the child's report travels on stderr.
    command
        .args(["-c", script.as_str()])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(done_writer);
    // SAFETY: the closure runs only in the forked child before exec and touches only the captured
    // integer, as the production pre_exec does.
    unsafe {
        command.pre_exec(move || {
            let flags = libc::fcntl(read_fd, libc::F_GETFD);
            if flags < 0 || libc::fcntl(read_fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC) < 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let error = spawn_bounded(
        vec![reader],
        move || {
            // The helper stays stalled before fork until the test releases it.
            let _ = gate.recv();
            command.spawn()
        },
        Duration::from_millis(100),
    )
    .await
    .expect_err("a spawn stalled before fork must miss its deadline");
    assert!(error.to_string().contains("did not exec"), "{error:#}");

    // The caller has given up. A released liveness number is free, and an unrelated descriptor
    // opened now takes it. An owner that still holds the number leaves it occupied.
    // SAFETY: fcntl on a descriptor number only reads its flags.
    if unsafe { libc::fcntl(read_fd, libc::F_GETFD) } < 0 {
        let unrelated = std::fs::File::open("/dev/null").expect("unrelated descriptor");
        // SAFETY: dup2 writes only the target number, which is free in this branch.
        let taken = unsafe { libc::dup2(unrelated.as_raw_fd(), read_fd) };
        assert_eq!(
            taken, read_fd,
            "the unrelated descriptor missed the freed number"
        );
    }

    release.send(()).expect("release the stalled spawn");
    let mut completed = String::new();
    done.read_to_string(&mut completed)
        .expect("read the child's completion");
    assert_eq!(
        completed, "done\n",
        "the child must find the liveness pipe at its number, not an unrelated descriptor"
    );
}
