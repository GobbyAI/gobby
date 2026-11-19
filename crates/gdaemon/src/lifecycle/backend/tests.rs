use super::*;
use crate::lease::NamedLock;
use crate::lifecycle::pid_file::{Role, claim_pid_file};
use crate::lifecycle::worker_channel::{
    WORKER_CHANNEL_FD_ENV, WORKER_CREDENTIAL_FD_ENV, WORKER_MARKER_ENV,
};
use crate::lifecycle::worker_handshake::{ProtocolViolation, WorkerCallError, WorkerHandshake};
use gobby_core::schema::SchemaIdentityContract;
use std::{collections::BTreeSet, sync::Mutex as StdMutex, time::Duration};
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
    let mut backend = BackendChild::spawn(&config, None).await?;
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
    let result = BackendChild::spawn(&config, None).await;
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
        let mut backend = BackendChild::spawn(&config, None).await?;
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
        vec![OwnedFd::from(reader)],
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

/// A supervisor in Rust mode over a backend command that exits at once.
fn rust_mode_supervisor(home: &tempfile::TempDir) -> BackendSupervisor {
    let pid_file = home.path().join("gobby.pid");
    let claim = claim_pid_file(&pid_file, Role::Daemon)
        .expect("pid file is writable")
        .expect("fresh pid file is claimable");
    let config =
        BackendConfig::from_command_json(home.path(), Some(r#"["/bin/sh","-c","exit 0"]"#))
            .expect("backend config");
    BackendSupervisor::with_runner(None, claim, config, RunnerMode::Rust)
}

/// A protocol violation in the handshake refuses the supervisor, and the refusal names the
/// handshake as its reason.
#[test]
fn handshake_sticky_failure_refuses_the_supervisor() -> anyhow::Result<()> {
    let home = tempfile::tempdir()?;
    let supervisor = rust_mode_supervisor(&home);
    let exit = supervisor
        .handle_handshake_failure(&WorkerCallError::Protocol(ProtocolViolation::Malformed));
    assert_eq!(exit, Some(SupervisorExit::Refused));
    let state = supervisor.subscribe();
    assert_eq!(
        state.borrow().clone(),
        BackendStatus::Refused {
            reason: "worker handshake failed: protocol".to_owned(),
        }
    );
    Ok(())
}

/// The mode a supervisor is built with is the mode it reports.
#[test]
fn with_runner_stores_the_mode() -> anyhow::Result<()> {
    let home = tempfile::tempdir()?;
    let supervisor = rust_mode_supervisor(&home);
    assert_eq!(supervisor.runner_mode(), RunnerMode::Rust);
    Ok(())
}

/// A child spawned with a generation source serves the source's first generation, and a child
/// spawned without one serves none.
#[tokio::test]
async fn spawn_assigns_generation_from_source() -> anyhow::Result<()> {
    let home = tempfile::tempdir()?;
    let config =
        BackendConfig::from_command_json(home.path(), Some(r#"["/bin/sh","-c","exit 0"]"#))?;
    let source = GenerationSource::default();
    let serving = BackendChild::spawn(&config, Some(&source)).await?;
    assert_eq!(serving.worker_generation(), Some(1));
    let plain = BackendChild::spawn(&config, None).await?;
    assert_eq!(plain.worker_generation(), None);
    Ok(())
}

/// The argument a probe child receives as its `$0`.
const PROBE_ARGUMENT: &str = "probe-argument";

/// Records what a probe child sees in `$GOBBY_HOME/probe.txt`: each descriptor above 2 that is
/// open at exec, the environment, its argument and how many follow it, and the text read from its
/// credential descriptor.
const PROBE_SCRIPT: &str = r#"p="$GOBBY_HOME/probe.txt"
i=3
while [ $i -le 1024 ]; do
  if [ -e "/dev/fd/$i" ]; then echo "open $i" >> "$p"; fi
  i=$((i + 1))
done
env >> "$p"
echo "arg $0" >> "$p"
echo "argc $#" >> "$p"
if [ -n "${GOBBY_WORKER_CREDENTIAL_FD-}" ]; then
  echo "credential $(cat "/dev/fd/$GOBBY_WORKER_CREDENTIAL_FD")" >> "$p"
fi
"#;

/// A backend configuration whose child runs the probe, with its home at `home`.
fn probe_config(home: &Path) -> anyhow::Result<BackendConfig> {
    let command = serde_json::to_string(&["/bin/sh", "-c", PROBE_SCRIPT, PROBE_ARGUMENT])?;
    BackendConfig::from_command_json(home, Some(&command))
}

/// Spawns the probe with `source` (a Rust-mode spawn when Some) and waits for it to exit. Returns
/// the child, which holds any worker attachment, and what the probe recorded.
async fn run_probe(
    config: &BackendConfig,
    source: Option<&GenerationSource>,
) -> anyhow::Result<(BackendChild, String)> {
    let mut child = BackendChild::spawn(config, source).await?;
    let status = timeout(Duration::from_secs(10), child.child.wait()).await??;
    child.finish_stderr().await;
    assert!(
        status.success(),
        "the probe exited with {status}: {}",
        child.last_line.borrow().as_str()
    );
    let recorded = std::fs::read_to_string(config.home.join("probe.txt"))?;
    Ok((child, recorded))
}

/// The value a probe recorded for an environment variable, when the variable was set.
fn recorded_env<'a>(recorded: &'a str, name: &str) -> Option<&'a str> {
    let prefix = format!("{name}=");
    recorded
        .lines()
        .find_map(|line| line.strip_prefix(prefix.as_str()))
}

/// A recorded environment value read as a descriptor number.
fn recorded_fd(recorded: &str, name: &str) -> Option<i32> {
    recorded_env(recorded, name).and_then(|value| value.parse::<i32>().ok())
}

/// The descriptors above 2 that the probe found open at exec.
fn recorded_descriptors(recorded: &str) -> BTreeSet<i32> {
    recorded
        .lines()
        .filter_map(|line| line.strip_prefix("open "))
        .map(|fd| {
            fd.parse::<i32>()
                .expect("the probe records descriptor numbers")
        })
        .collect()
}

/// The text the probe read from its credential descriptor, when it read one.
fn recorded_credential(recorded: &str) -> Option<&str> {
    recorded
        .lines()
        .find_map(|line| line.strip_prefix("credential "))
}

/// A Rust-mode child inherits exactly three descriptors above 2: its liveness pipe, the child end of
/// the worker channel and the credential pipe. The two worker descriptors are named by variables.
#[tokio::test]
async fn worker_link_child_end_is_the_only_inherited_channel() -> anyhow::Result<()> {
    let home = tempfile::tempdir()?;
    let config = probe_config(home.path())?;
    let (_child, recorded) = run_probe(&config, Some(&GenerationSource::default())).await?;
    let supervisor_fd = recorded_fd(&recorded, "GOBBY_SUPERVISOR_FD");
    let channel_fd = recorded_fd(&recorded, WORKER_CHANNEL_FD_ENV);
    let credential_fd = recorded_fd(&recorded, WORKER_CREDENTIAL_FD_ENV);
    assert!(
        channel_fd.is_some() && credential_fd.is_some(),
        "a Rust-mode child is named its channel and credential descriptors"
    );
    let expected: BTreeSet<i32> = [supervisor_fd, channel_fd, credential_fd]
        .into_iter()
        .flatten()
        .collect();
    assert_eq!(
        recorded_descriptors(&recorded),
        expected,
        "exactly the liveness, channel and credential descriptors are inherited"
    );
    Ok(())
}

/// The credential reaches a Rust-mode child on its own inherited pipe. The child reads the
/// attached credential, and the credential is in neither the argument vector nor any environment
/// value.
#[tokio::test]
async fn rust_mode_credential_rides_an_inherited_pipe_only() -> anyhow::Result<()> {
    let home = tempfile::tempdir()?;
    let config = probe_config(home.path())?;
    let (child, recorded) = run_probe(&config, Some(&GenerationSource::default())).await?;
    assert!(
        child.worker.is_some(),
        "a Rust-mode spawn attaches a worker with a credential"
    );
    let credential = child
        .worker
        .as_ref()
        .map(|worker| worker.credential.clone())
        .expect("attached above");
    // Compared without printing either value, so a failure never echoes the credential.
    assert!(
        recorded_credential(&recorded) == Some(credential.as_str()),
        "the child reads the attached credential from its own pipe"
    );
    assert_eq!(
        credential.len(),
        43,
        "32 random bytes, encoded as URL-safe base64 without padding"
    );
    assert!(
        recorded
            .lines()
            .filter(|line| !line.starts_with("credential "))
            .all(|line| !line.contains(credential.as_str())),
        "the credential is in an environment value or the probe's record outside its pipe"
    );
    assert!(
        !config
            .command
            .iter()
            .any(|arg| arg.to_string_lossy().contains(credential.as_str())),
        "the credential is in the argument vector"
    );
    Ok(())
}

/// The marker names a Rust-mode start only. A Python-default spawn sets no marker.
#[tokio::test]
async fn rust_mode_spawn_sets_the_marker_and_python_default_does_not() -> anyhow::Result<()> {
    let rust_home = tempfile::tempdir()?;
    let python_home = tempfile::tempdir()?;
    let (_rust, rust_recorded) = run_probe(
        &probe_config(rust_home.path())?,
        Some(&GenerationSource::default()),
    )
    .await?;
    let (_python, python_recorded) = run_probe(&probe_config(python_home.path())?, None).await?;
    assert_eq!(
        recorded_env(&rust_recorded, WORKER_MARKER_ENV),
        Some("1"),
        "a Rust-mode start is marked"
    );
    assert_eq!(
        recorded_env(&python_recorded, WORKER_MARKER_ENV),
        None,
        "a Python-default start is not marked"
    );
    Ok(())
}

/// The shell's own exports, which every command it runs carries. They are not the spawn's.
const SHELL_EXPORTS: [&str; 4] = ["_", "OLDPWD", "PWD", "SHLVL"];

/// Whether `name` is a plain environment variable name.
fn is_plain_name(name: &str) -> bool {
    !name.is_empty() && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
}

/// Whether the environment comparison pins `name`. The shell's exports are left out, since they
/// are the shell's. So are the `DYLD_` names: macOS strips them from the environment of a
/// protected binary such as `/bin/sh` before the child runs, so the child never carries them.
fn is_pinned_name(name: &str) -> bool {
    is_plain_name(name) && !name.starts_with("DYLD_") && !SHELL_EXPORTS.contains(&name)
}

/// The environment names a recorded `env` listing carries, as the pin sees them.
fn recorded_names(recorded: &str) -> BTreeSet<String> {
    recorded
        .lines()
        .filter_map(|line| line.split_once('='))
        .map(|(name, _)| name)
        .filter(|name| is_pinned_name(name))
        .map(str::to_owned)
        .collect()
}

/// The environment names a Python-default spawn gives its child. The child inherits the test
/// process's environment. The spawn adds the home and the liveness descriptor, and removes the two
/// handles that a backend must not inherit by name.
fn expected_python_default_names() -> BTreeSet<String> {
    let mut names: BTreeSet<String> = std::env::vars_os()
        .filter_map(|(name, _)| name.into_string().ok())
        .filter(|name| is_pinned_name(name))
        .collect();
    names.insert("GOBBY_HOME".to_owned());
    names.insert("GOBBY_SUPERVISOR_FD".to_owned());
    names.remove("GOBBY_SINGLETON_LOCK_FD");
    names.remove("GOBBY_PARENT_FD");
    names
}

/// A Python-default spawn is what it was before the worker channel: its child receives the
/// configured argument and no other, its environment names are the inherited set plus the spawn's
/// own, its only inherited descriptor above 2 is the liveness pipe, and it carries no worker
/// variables.
#[tokio::test]
async fn python_default_spawn_is_unchanged() -> anyhow::Result<()> {
    let home = tempfile::tempdir()?;
    let config = probe_config(home.path())?;
    let expected_names = expected_python_default_names();
    let (child, recorded) = run_probe(&config, None).await?;
    assert_eq!(child.worker_generation(), None, "no generation is drawn");
    assert!(child.worker.is_none(), "no worker is attached");
    assert_eq!(
        recorded_names(&recorded),
        expected_names,
        "the environment names are the inherited set plus the home and the liveness descriptor"
    );
    let supervisor_fd = recorded_fd(&recorded, "GOBBY_SUPERVISOR_FD");
    assert!(supervisor_fd.is_some(), "the liveness pipe is named");
    assert_eq!(
        recorded_descriptors(&recorded),
        supervisor_fd.into_iter().collect::<BTreeSet<i32>>(),
        "the liveness pipe is the only descriptor above 2"
    );
    for name in [
        WORKER_MARKER_ENV,
        WORKER_CHANNEL_FD_ENV,
        WORKER_CREDENTIAL_FD_ENV,
    ] {
        assert_eq!(
            recorded_env(&recorded, name),
            None,
            "{name} is set only for a Rust-mode start"
        );
    }
    let home_text = home.path().display().to_string();
    assert_eq!(
        recorded_env(&recorded, "GOBBY_HOME"),
        Some(home_text.as_str()),
        "the home is named as before"
    );
    let argument = format!("arg {PROBE_ARGUMENT}");
    assert!(
        recorded.lines().any(|line| line == argument.as_str()),
        "the child's first argument is the configured one"
    );
    assert!(
        recorded.lines().any(|line| line == "argc 0"),
        "no argument follows the configured one"
    );
    Ok(())
}

/// Each spawn draws its worker generation exactly once: the child serves the drawn value, and the
/// supervisor's source has issued exactly one.
#[tokio::test]
async fn one_spawn_draws_one_generation() -> anyhow::Result<()> {
    let home = tempfile::tempdir()?;
    let supervisor = rust_mode_supervisor(&home);
    let child = BackendChild::spawn(&supervisor.config, supervisor.spawn_generations()).await?;
    assert_eq!(
        child.worker_generation(),
        Some(1),
        "the first spawn serves the first generation"
    );
    assert_eq!(
        supervisor.issued_generations(),
        1,
        "one spawn draws one generation"
    );
    Ok(())
}

/// Counts the child starts the supervisor reports. Returns once `want` have been seen, or once the
/// state channel closes.
async fn count_starts(states: &mut watch::Receiver<BackendStatus>, want: usize) -> usize {
    let mut seen = 0;
    loop {
        if matches!(*states.borrow_and_update(), BackendStatus::Starting { .. }) {
            seen += 1;
            if seen == want {
                return seen;
            }
        }
        if states.changed().await.is_err() {
            return seen;
        }
    }
}

/// A Rust-mode child that ends before it answers its handshake is a crash. The supervisor starts
/// the next child, and each start draws a new generation.
#[tokio::test]
async fn rust_mode_channel_loss_respawns_with_a_new_generation() -> anyhow::Result<()> {
    let home = tempfile::tempdir()?;
    let mut supervisor = rust_mode_supervisor(&home);
    // The probe child exits at once but stays unreaped, so a drain would hold it for its whole
    // grace budget. This test is about the respawn, so it shortens that budget.
    supervisor.config.drain_timeout = Duration::from_secs(1);
    let mut states = supervisor.subscribe();
    let (stop, stop_rx) = watch::channel(false);
    let observed = async {
        let starts = timeout(Duration::from_secs(30), count_starts(&mut states, 2))
            .await
            .unwrap_or(0);
        // Stop the supervisor whether or not a restart was seen, so that its run ends.
        let _ = stop.send(true);
        starts
    };
    let (exit, starts) = tokio::join!(supervisor.run(stop_rx), observed);
    exit?;
    assert!(
        starts >= 2,
        "the supervisor started {starts} children; a lost handshake must respawn the child"
    );
    assert!(
        supervisor.issued_generations() >= 2,
        "each start draws a generation, and {} were drawn",
        supervisor.issued_generations()
    );
    Ok(())
}

/// A Rust-mode start of the real worker answers the handshake over its attached channel with the
/// embedded schema identity and the three commands. The worker's boot does no database work, so the
/// isolated test hub is named only through the environment.
#[tokio::test]
async fn rust_mode_handshake_against_real_python() -> anyhow::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let repo_root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let python = repo_root.join(".venv/bin/python");
    assert!(
        python.is_file(),
        "the real-worker test needs the checkout's interpreter at {}",
        python.display()
    );
    let database_url =
        std::env::var("DATABASE_URL").context("DATABASE_URL must name the isolated test hub")?;
    let home = tempfile::tempdir()?;
    let files_home = home.path().join("files-home");
    std::fs::create_dir(&files_home)?;
    let bootstrap = home.path().join("bootstrap.yaml");
    std::fs::write(
        &bootstrap,
        format!(
            "database_url: {}\nfiles_home: {}\n",
            serde_json::to_string(&database_url)?,
            serde_json::to_string(&files_home.display().to_string())?,
        ),
    )?;
    std::fs::set_permissions(&bootstrap, std::fs::Permissions::from_mode(0o600))?;
    let command = serde_json::to_string(&[
        python.display().to_string(),
        "-m".to_owned(),
        "gobby.runner".to_owned(),
    ])?;
    let mut config = BackendConfig::from_command_json(home.path(), Some(&command))?;
    config
        .environment
        .insert("PYTHONPATH".into(), repo_root.join("src").into_os_string());
    for (name, value) in [
        ("DATABASE_URL", database_url.as_str()),
        ("GOBBY_TEST_PROTECT", "1"),
        ("GOBBY_ALLOW_WORKTREE_DAEMON", "1"),
    ] {
        config.environment.insert(name.into(), value.into());
    }
    let mut child = BackendChild::spawn(&config, Some(&GenerationSource::default())).await?;
    assert!(
        child.worker.is_some(),
        "a Rust-mode spawn attaches the worker channel"
    );
    let mut attachment = child.worker.take().expect("attached above");
    let supervisor = rust_mode_supervisor(&home);
    let handshake = supervisor
        .handshake_worker_for(
            RunnerMode::Rust,
            &mut attachment.link,
            &mut attachment.session,
            &attachment.credential,
        )
        .await
        .map_err(|error| anyhow::anyhow!("the Rust handshake failed: {error:?}"))?;
    let result = match handshake {
        WorkerHandshake::Ready(result) => result,
        other => anyhow::bail!("the Rust handshake did not complete: {other:?}"),
    };
    assert_eq!(
        result.schema_identity,
        serde_json::to_value(SchemaIdentityContract::embedded())
            .expect("the installed schema contract serializes"),
        "the worker reports the embedded schema identity"
    );
    assert_eq!(
        result.commands,
        ["handshake", "initialize", "invoke"],
        "the worker serves the three commands"
    );
    // Closing the parent's end ends the worker's serve loop. The worker must then exit on its own,
    // and the test reaps it, so that no worker process outlives the test.
    drop(attachment);
    let status = timeout(Duration::from_secs(30), child.child.wait())
        .await
        .context("the worker did not exit after its channel closed")??;
    child.finish_stderr().await;
    assert!(
        status.success(),
        "the worker exited with {status} after its channel closed"
    );
    Ok(())
}
