//! Teardown against processes the test itself starts: which of them a shutdown may signal.

use std::fs;
use std::io::{BufRead, BufReader};
use std::os::unix::process::CommandExt;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use super::process::{Identity, pid_alive, signal_pid};
use super::{FullDaemon, FullDaemonError};

/// A process the test started in its own process group, killed and reaped on drop.
struct Owned(Child);

impl Owned {
    fn sleeper() -> Self {
        let child = Command::new("/bin/sleep")
            .arg("600")
            .current_dir("/")
            .process_group(0)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn sleep");
        Self(child)
    }

    fn pid(&self) -> u32 {
        self.0.id()
    }
}

impl Drop for Owned {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

/// One pid the test started, force-stopped on drop whatever the test's outcome.
struct Stopped(u32);

impl Drop for Stopped {
    fn drop(&mut self) {
        signal_pid(self.0, false);
    }
}

/// A run with a live root and no daemon child, worker or host: only what a test sets is stopped.
fn bare_run() -> FullDaemon {
    let root = tempfile::tempdir().expect("tempdir");
    let root_canonical = fs::canonicalize(root.path()).expect("canonical root");
    FullDaemon {
        root: Some(root),
        host_dir: None,
        keep: false,
        database: None,
        database_name: None,
        child: None,
        daemon_pid: 0,
        worker: None,
        front_door: String::new(),
        operator_key: String::new(),
        ready_ceiling: Duration::from_secs(1),
        readiness_path: String::new(),
        watch: None,
        relays: None,
        root_canonical,
    }
}

/// A shell that leads its own process group and dies on SIGTERM, with one member of that group
/// that ignores SIGTERM. Returns the leader's pid and the member's pid. A thread reaps the leader
/// the moment it exits, as init does for a real worker, so it never lingers as a zombie.
fn leader_with_a_member_that_ignores_term() -> (u32, u32) {
    let mut leader = Command::new("/bin/sh")
        .args(["-c", "(trap '' TERM; exec /bin/sleep 600) & echo $!; wait"])
        .current_dir("/")
        .process_group(0)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn the group leader");
    let stdout = leader.stdout.take().expect("the leader's stdout");
    let mut line = String::new();
    BufReader::new(stdout)
        .read_line(&mut line)
        .expect("read the member's pid");
    let member = line.trim().parse::<u32>().expect("a member pid");
    let pid = leader.id();
    std::thread::spawn(move || {
        let _ = leader.wait();
    });
    (pid, member)
}

/// Waits up to five seconds for the process to be gone: a forced stop takes a moment to land.
fn gone(pid: u32) -> bool {
    let deadline = Instant::now() + Duration::from_secs(5);
    while pid_alive(pid) && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(20));
    }
    !pid_alive(pid)
}

/// Once gdaemon's child handle is gone, its recorded pid may belong to any process. A shutdown
/// then must not find a worker through that pid: here the pid is the test's own, and the test's
/// group-leading child is what such a lookup would find and stop.
#[test]
fn a_shutdown_without_a_daemon_child_finds_no_worker_through_the_recorded_pid() {
    let sleeper = Owned::sleeper();
    let mut run = bare_run();
    run.daemon_pid = std::process::id();
    let result = run.teardown();
    assert!(
        pid_alive(sleeper.pid()),
        "a child of the process that now holds the recorded pid must not be stopped: {result:?}"
    );
}

/// A member of the worker's group that outlives the worker is a leak: teardown stops it and fails.
#[test]
fn a_worker_group_member_that_outlives_the_worker_fails_teardown() {
    let (leader, member) = leader_with_a_member_that_ignores_term();
    let _stopped = (Stopped(member), Stopped(leader));
    let mut run = bare_run();
    run.worker = Identity::observe(leader);
    assert!(run.worker.is_some(), "the leader is alive");
    let result = run.teardown();
    assert!(
        matches!(&result, Err(FullDaemonError::Teardown(text)) if text.contains(&format!("{member}"))),
        "teardown must fail naming the surviving member {member}, got {result:?}"
    );
    assert!(
        gone(member),
        "teardown must stop the surviving member {member}"
    );
}
