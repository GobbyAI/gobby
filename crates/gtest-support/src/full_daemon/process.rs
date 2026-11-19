//! Process liveness, stop requests and worker lookup for the full-daemon harness. Unix uses
//! kill(2), killpg(2) and ps(1). Windows uses tasklist and taskkill, which ship with the OS, since
//! plain std has no liveness probe, no process groups and no process-table walk.

use std::process::{Child, Command};
use std::thread::sleep;
use std::time::{Duration, Instant};

#[cfg(unix)]
use nix::errno::Errno;
#[cfg(unix)]
use nix::sys::signal::{Signal, kill, killpg};
#[cfg(unix)]
use nix::unistd::Pid;

use super::{FullDaemonError, POLL};

/// Whether a process with this pid exists and is not a zombie. EPERM counts as alive.
#[cfg(unix)]
pub fn pid_alive(pid: u32) -> bool {
    // Pid 0 names the caller's own process group to kill(2), so it is never a process of ours.
    let Ok(raw) = i32::try_from(pid) else {
        return false;
    };
    if raw <= 0 {
        return false;
    }
    match kill(Pid::from_raw(raw), None) {
        Ok(()) => !is_zombie(pid),
        Err(Errno::EPERM) => true,
        Err(_) => false,
    }
}

/// Windows liveness through tasklist, the shared check in the database module.
#[cfg(windows)]
pub fn pid_alive(pid: u32) -> bool {
    crate::database::process_alive(pid)
}

/// Whether the process is a zombie, by its `ps` state column.
#[cfg(unix)]
fn is_zombie(pid: u32) -> bool {
    Command::new("ps")
        .args(["-o", "stat=", "-p", &pid.to_string()])
        .output()
        .map(|output| {
            String::from_utf8_lossy(&output.stdout)
                .trim()
                .starts_with('Z')
        })
        .unwrap_or(false)
}

/// A process named by its pid and its start time as `ps` prints it. The kernel gives a reused pid a
/// later start time, so an identity taken earlier tells the old process from the new one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Identity {
    /// The process id.
    pub pid: u32,
    /// The start time read when the identity was taken.
    start: String,
}

impl Identity {
    /// The identity of the process with this pid now, or `None` when no process has that pid.
    pub fn observe(pid: u32) -> Option<Self> {
        let start = start_time(pid)?;
        Some(Self { pid, start })
    }

    /// Whether the process with this pid still has the start time this identity recorded.
    pub fn is_same_process(&self) -> bool {
        start_time(self.pid).as_deref() == Some(self.start.as_str())
    }
}

/// The start time `ps` prints for `pid`, or `None` when no process has that pid.
#[cfg(unix)]
fn start_time(pid: u32) -> Option<String> {
    let output = Command::new("ps")
        .args(["-o", "lstart=", "-p", &pid.to_string()])
        .output()
        .ok()?;
    let start = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    (!start.is_empty()).then_some(start)
}

/// Plain std reads no start time on Windows, so no identity is taken there and nothing is signalled
/// through one. The harness refuses to start on Windows before any process it would stop exists.
#[cfg(windows)]
fn start_time(_pid: u32) -> Option<String> {
    None
}

/// Asks a process group (Unix) or a process tree (Windows) to stop. `graceful` sends SIGTERM on
/// Unix; Windows has no graceful signal in plain std, so it always force-stops the tree.
#[cfg(unix)]
pub fn signal_group(pid: u32, graceful: bool) {
    let Ok(raw) = i32::try_from(pid) else {
        return;
    };
    let signal = if graceful {
        Signal::SIGTERM
    } else {
        Signal::SIGKILL
    };
    let _ = killpg(Pid::from_raw(raw), signal);
}

/// Windows: `taskkill /T /F` ends the process and its children. There is no graceful stop.
#[cfg(windows)]
pub fn signal_group(pid: u32, _graceful: bool) {
    taskkill(pid, true);
}

/// Asks one process (not its group) to stop. Windows force-stops it with `taskkill /F`.
#[cfg(unix)]
pub fn signal_pid(pid: u32, graceful: bool) {
    let Ok(raw) = i32::try_from(pid) else {
        return;
    };
    let signal = if graceful {
        Signal::SIGTERM
    } else {
        Signal::SIGKILL
    };
    let _ = kill(Pid::from_raw(raw), signal);
}

/// Windows: `taskkill /F` for the single process.
#[cfg(windows)]
pub fn signal_pid(pid: u32, _graceful: bool) {
    taskkill(pid, false);
}

/// Runs `taskkill /PID <pid> /F`, adding `/T` for the tree. Its exit status is not checked: the
/// caller polls liveness afterwards, which is the proof that matters.
#[cfg(windows)]
fn taskkill(pid: u32, tree: bool) {
    let mut command = Command::new("taskkill");
    command.args(["/PID", &pid.to_string(), "/F"]);
    if tree {
        command.arg("/T");
    }
    let _ = command.output();
}

/// The `ps` column that names a process: `ucomm` on macOS, `comm` elsewhere (Linux `ps`).
#[cfg(all(unix, target_os = "macos"))]
pub const NAME_COLUMN: &str = "ucomm";
/// The `ps` column that names a process on Linux, where `ucomm` does not exist.
#[cfg(all(unix, not(target_os = "macos")))]
pub const NAME_COLUMN: &str = "comm";

/// The watchdog's script. Arguments: `$1` gdaemon's group, `$2` the gterm pid file, `$3` the `ps`
/// name column. It ignores signals and waits on stdin. A line `stop` is a clean teardown, so it
/// exits without signalling. End of input means the owning test process died without teardown
/// (SIGKILL, a crash, a killed test run): then it stops the daemon's group, each Python worker
/// group among its children, and the gterm host the pid file names when that pid is a gterm. It
/// sends TERM, waits 2 s, then KILL.
#[cfg(unix)]
const WATCHDOG_SCRIPT: &str = r#"trap '' INT TERM HUP
read -r line
if [ "$line" = stop ]; then exit 0; fi
namecol=$3
workers() { ps -A -o pid= -o ppid= -o pgid= 2>/dev/null | awk -v d="$1" '$2 == d && $1 == $3 { print $1 }'; }
host_name() { ps -o "$namecol"= -p "$1" 2>/dev/null | tr -d ' \t'; }
host=$(cat "$2" 2>/dev/null)
case "$host" in ''|*[!0-9]*) host= ;; esac
found=$(workers "$1")
kill -TERM -- -"$1" 2>/dev/null
for w in $found; do kill -TERM -- -"$w" 2>/dev/null; done
if [ -n "$host" ]; then
  case "$(host_name "$host")" in gterm|gterm-*) kill -TERM "$host" 2>/dev/null ;; esac
fi
sleep 2
kill -KILL -- -"$1" 2>/dev/null
for w in $found; do kill -KILL -- -"$w" 2>/dev/null; done
if [ -n "$host" ]; then
  case "$(host_name "$host")" in gterm|gterm-*) kill -KILL "$host" 2>/dev/null ;; esac
fi
exit 0
"#;

/// Kills a daemon's process groups, and its gterm host, if the test process dies without tearing
/// the daemon down. The watchdog runs in its own process group, so a signal aimed at the test's
/// group does not reach it, and it acts on the end of its stdin, which the kernel closes when the
/// test process exits for any reason. Teardown writes `stop` first, so a clean run kills nothing.
#[cfg(unix)]
#[derive(Debug)]
pub struct GroupWatch {
    child: Option<Child>,
}

#[cfg(unix)]
impl GroupWatch {
    /// Starts the watchdog for the process group led by `group` (gdaemon, which was spawned with
    /// `process_group(0)`), and for the gterm host whose pid `host_pid_file` names.
    pub fn start(group: u32, host_pid_file: &std::path::Path) -> Result<Self, FullDaemonError> {
        use std::os::unix::process::CommandExt;
        use std::process::Stdio;
        let child = Command::new("/bin/sh")
            .arg("-c")
            .arg(WATCHDOG_SCRIPT)
            .arg("watchdog")
            .arg(group.to_string())
            .arg(host_pid_file)
            .arg(NAME_COLUMN)
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0)
            .spawn()
            .map_err(|error| {
                FullDaemonError::Io(format!("start the teardown watchdog: {error}"))
            })?;
        Ok(Self { child: Some(child) })
    }

    /// The clean stop: the watchdog reads `stop`, exits without signalling, and is reaped.
    pub fn stop(&mut self) {
        let Some(mut child) = self.child.take() else {
            return;
        };
        if let Some(mut stdin) = child.stdin.take() {
            use std::io::Write;
            let _ = writeln!(stdin, "stop");
        }
        let _ = child.wait();
    }
}

#[cfg(unix)]
impl Drop for GroupWatch {
    fn drop(&mut self) {
        self.stop();
    }
}

/// Windows: no process group in plain std, so the watchdog is unbuilt. `start` refuses.
#[cfg(windows)]
#[derive(Debug)]
pub struct GroupWatch;

#[cfg(windows)]
impl GroupWatch {
    /// Refuses on Windows: the kill-safe teardown needs a process group, which plain std lacks.
    pub fn start(_group: u32, _host_pid_file: &std::path::Path) -> Result<Self, FullDaemonError> {
        Err(FullDaemonError::Unsupported(
            "the kill-safe teardown watchdog needs a process group, which plain std lacks on Windows".into(),
        ))
    }

    /// Nothing to stop on Windows.
    pub fn stop(&mut self) {}
}

/// SIGTERM (or the Windows force stop) to gdaemon's group, wait for its exit, then the forced
/// stop of the group after the grace period.
pub fn stop_daemon(child: &mut Child, grace: Duration) -> Result<(), FullDaemonError> {
    let pid = child.id();
    signal_group(pid, true);
    let deadline = Instant::now() + grace;
    loop {
        if child
            .try_wait()
            .map_err(|error| FullDaemonError::Teardown(format!("poll gdaemon: {error}")))?
            .is_some()
        {
            return Ok(());
        }
        if Instant::now() >= deadline {
            signal_group(pid, false);
            child
                .wait()
                .map_err(|error| FullDaemonError::Teardown(format!("reap gdaemon: {error}")))?;
            return Ok(());
        }
        sleep(POLL);
    }
}

/// Stops the worker's group, waiting up to `grace` for it to exit before the forced stop. Nothing
/// is signalled unless the process at the worker's pid is still the one recorded at readiness.
pub fn stop_worker(worker: &Identity, grace: Duration) {
    if !worker.is_same_process() {
        return;
    }
    signal_group(worker.pid, true);
    let deadline = Instant::now() + grace;
    while worker.is_same_process() && pid_alive(worker.pid) && Instant::now() < deadline {
        sleep(POLL);
    }
    if worker.is_same_process() && pid_alive(worker.pid) {
        signal_group(worker.pid, false);
    }
}

#[cfg(all(test, unix))]
mod tests {
    use std::os::unix::process::CommandExt;
    use std::process::{Child, Command, Stdio};
    use std::time::Duration;

    use super::*;

    /// A `sleep` the test started in its own process group, killed and reaped on drop.
    struct Sleeper(Child);

    impl Sleeper {
        fn start() -> Self {
            let child = Command::new("sleep")
                .arg("600")
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

        fn running(&mut self) -> bool {
            matches!(self.0.try_wait(), Ok(None))
        }
    }

    impl Drop for Sleeper {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }

    #[test]
    fn stop_worker_does_not_signal_a_pid_with_another_start_time() {
        let mut sleeper = Sleeper::start();
        let recorded = Identity {
            pid: sleeper.pid(),
            start: "Thu Jan  1 00:00:00 1970".to_owned(),
        };
        stop_worker(&recorded, Duration::from_millis(200));
        assert!(
            sleeper.running(),
            "a worker whose start time differs from the recorded one must not be signalled"
        );
    }

    #[test]
    fn stop_worker_stops_the_process_its_identity_names() {
        let mut sleeper = Sleeper::start();
        let identity = Identity::observe(sleeper.pid()).expect("the sleep is alive");
        stop_worker(&identity, Duration::from_secs(5));
        assert!(!sleeper.running(), "the identified worker must be stopped");
    }
}
