//! The shared SRT violation monitor (port of `srt_monitor.py::ensure_srt_monitor` and
//! `srt_socket_paths.py`). One broker per runner identity serves every darwin SRT run;
//! each run receives the version-addressed socket it must reach.

#[cfg(test)]
#[path = "monitor/tests.rs"]
mod tests;

use super::{paths, verify::SRT_RELEASE, wait_bounded};
use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs::{self, DirBuilder, File, OpenOptions},
    io::{self, BufRead, BufReader, Read, Write},
    os::unix::{
        ffi::OsStrExt,
        fs::{DirBuilderExt, FileTypeExt, MetadataExt, OpenOptionsExt, PermissionsExt},
        io::AsRawFd,
        net::UnixStream,
        process::CommandExt,
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    thread,
    time::Duration,
};

const SOCKET_DIR_OVERRIDE: &str = "GOBBY_SRT_SOCKET_DIR";
/// Reserves 40 bytes of macOS's 104-byte `sun_path` for SRT's PID and sequence name.
const SOCKET_DIRECTORY_BUDGET: usize = 63;
const LOCK_NAME: &str = "violations.lock";
const LOG_NAME: &str = "violations-monitor.log";
const PROBE_REQUEST: &[u8] = b"{\"suffix\":\"_gobbyprobe_SBX\"}\n";
const PROBE_LINE_LIMIT: u64 = 4096;
const PROBE_TIMEOUT: Duration = Duration::from_secs(30);
const READY_TIMEOUT: Duration = Duration::from_secs(30);
const RECOVERY_TIMEOUT: Duration = Duration::from_secs(30);
const TERMINATE_TIMEOUT: Duration = Duration::from_secs(5);
const LOG_TAIL_BYTES: usize = 4096;

/// Why a probe of the shared broker failed, mirroring the oracle's exception classes.
#[derive(Debug)]
enum Probe {
    /// Nothing listens on the path: no socket file, or a stale one.
    Absent,
    /// The broker answered with another runner identity.
    Identity,
    /// The broker timed out, refused the request, or sent an invalid record.
    Unresponsive(anyhow::Error),
}

impl std::fmt::Display for Probe {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Absent => f.write_str("no monitor is listening"),
            Self::Identity => {
                f.write_str("Shared SRT monitor identity mismatch; coordinate monitor cutover")
            }
            Self::Unresponsive(error) => write!(f, "{error:#}"),
        }
    }
}

/// The socket a run must reach, starting or recovering the shared broker under the
/// directory lock. A live incompatible broker is never replaced under its subscribers.
pub(super) fn ensure(
    gobby: &Path,
    node: &Path,
    runner: &Path,
    env: &BTreeMap<String, String>,
    run_key: &str,
) -> Result<PathBuf> {
    let directory = mux_tmpdir(gobby, env)?;
    // Version-addressed sockets let the old cohort drain during a component update.
    let path = directory.join(format!("v-{}", &SRT_RELEASE.runner_sha256[..12]));
    let _lock = lock_directory(&directory)?;
    validate_run_key(run_key)?;
    if has_backlog(&directory, run_key) {
        recover(gobby, node, runner, &directory, env, run_key)?;
    }
    match probe(&path) {
        Ok(()) => return Ok(path),
        Err(Probe::Absent) => remove_stale_socket(&path)?,
        Err(Probe::Identity) => bail!("{}", Probe::Identity),
        Err(error @ Probe::Unresponsive(_)) => bail!(
            "Shared SRT monitor at {} is unresponsive; inspect {} and coordinate recovery of its cohort: {error}",
            path.display(),
            directory.join(LOG_NAME).display()
        ),
    }
    start(gobby, node, runner, &directory, &path, env)?;
    Ok(path)
}

/// `srt_mux_tmpdir`: the private directory that holds the monitor socket for this home.
pub(super) fn mux_tmpdir(gobby: &Path, env: &BTreeMap<String, String>) -> Result<PathBuf> {
    let directory = socket_directory(gobby, env);
    let short_root = shared_root();
    if directory.starts_with(&short_root) {
        ensure_private_directory(&short_root)?;
    }
    ensure_private_directory(&directory)?;
    Ok(directory)
}

/// `srt_socket_directory`: the override when set, else the gobby-home root when its path
/// fits the socket budget, else a directory keyed by the home under the shared root.
fn socket_directory(gobby: &Path, env: &BTreeMap<String, String>) -> PathBuf {
    if let Some(raw) = env.get(SOCKET_DIR_OVERRIDE).filter(|raw| !raw.is_empty()) {
        return resolve_parent(Path::new(raw));
    }
    let home_root = resolve_parent(&gobby.join("runtime").join("srt-sock"));
    if home_root.as_os_str().len() <= SOCKET_DIRECTORY_BUDGET {
        return home_root;
    }
    let home = paths::resolve_nonstrict(gobby);
    shared_root().join(super::hash16(home.as_os_str().as_bytes()))
}

/// The shared `/tmp/gobby-srt-<uid>` root, with its parent resolved.
fn shared_root() -> PathBuf {
    resolve_parent(&Path::new("/tmp").join(format!("gobby-srt-{}", current_uid())))
}

/// Resolves a path's parent, leaving its final component unfollowed.
fn resolve_parent(path: &Path) -> PathBuf {
    match (path.parent(), path.file_name()) {
        (Some(parent), Some(name)) => paths::resolve_nonstrict(parent).join(name),
        _ => paths::resolve_nonstrict(path),
    }
}

fn ensure_private_directory(directory: &Path) -> Result<()> {
    DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(directory)
        .with_context(|| format!("create {}", directory.display()))?;
    let handle = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW)
        .open(directory)?;
    let info = handle.metadata()?;
    if !info.is_dir() || info.uid() != current_uid() {
        bail!("Unsafe SRT socket directory: {}", directory.display());
    }
    handle.set_permissions(fs::Permissions::from_mode(0o700))?;
    Ok(())
}

/// Holds `violations.lock` for the caller's scope. The lock serializes every writer of
/// the directory, so it is held until `ensure` returns.
fn lock_directory(directory: &Path) -> Result<File> {
    let lock = OpenOptions::new()
        .read(true)
        .append(true)
        .create(true)
        .open(directory.join(LOCK_NAME))?;
    lock.set_permissions(fs::Permissions::from_mode(0o600))?;
    // SAFETY: `lock` owns a valid descriptor for the whole call; flock only reads it.
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX) } != 0 {
        return Err(io::Error::last_os_error()).context("lock SRT violation directory");
    }
    Ok(lock)
}

fn validate_run_key(run_key: &str) -> Result<()> {
    let valid = (1..=128).contains(&run_key.len())
        && run_key
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-');
    if !valid {
        bail!("Invalid shared SRT monitor run key");
    }
    Ok(())
}

fn has_backlog(directory: &Path, run_key: &str) -> bool {
    [
        format!("{run_key}.srt-backlog.jsonl"),
        format!("{run_key}.srt-backlog.jsonl.active"),
    ]
    .iter()
    .any(|name| fs::symlink_metadata(directory.join(name)).is_ok())
}

/// `--recover` replays a run's backlog into the broker's record. Exit 2 means a live
/// monitor owns the backlog, which is never recovered under its subscribers.
fn recover(
    gobby: &Path,
    node: &Path,
    runner: &Path,
    directory: &Path,
    env: &BTreeMap<String, String>,
    run_key: &str,
) -> Result<()> {
    let mut child = Command::new(node)
        .arg(runner)
        .arg("--recover")
        .arg(directory)
        .arg(violations_dir(gobby))
        .arg(run_key)
        .env_clear()
        .env("PATH", path_env(env))
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::from(open_log(directory)?))
        .spawn()
        .context("start SRT backlog recovery")?;
    let Some(status) = wait_bounded(&mut child, RECOVERY_TIMEOUT).context("wait for recovery")?
    else {
        bail!("SRT backlog recovery timed out for run {run_key}");
    };
    match status.code() {
        Some(0) => Ok(()),
        Some(2) => bail!("Run {run_key} has a backlog owned by a live SRT monitor"),
        _ => bail!(
            "SRT backlog recovery failed for run {run_key}; inspect {}",
            directory.join(LOG_NAME).display()
        ),
    }
}

/// `_connect`: the broker must accept the probe and answer with the pinned identity.
fn probe(path: &Path) -> std::result::Result<(), Probe> {
    let mut client = UnixStream::connect(path).map_err(|error| match error.kind() {
        io::ErrorKind::NotFound | io::ErrorKind::ConnectionRefused => Probe::Absent,
        _ => Probe::Unresponsive(error.into()),
    })?;
    let unresponsive = |error: io::Error| Probe::Unresponsive(error.into());
    client
        .set_read_timeout(Some(PROBE_TIMEOUT))
        .map_err(unresponsive)?;
    client
        .set_write_timeout(Some(PROBE_TIMEOUT))
        .map_err(unresponsive)?;
    client.write_all(PROBE_REQUEST).map_err(unresponsive)?;
    let mut line = Vec::new();
    BufReader::new(&client)
        .take(PROBE_LINE_LIMIT)
        .read_until(b'\n', &mut line)
        .map_err(unresponsive)?;
    let record: Value =
        serde_json::from_slice(&line).map_err(|error| Probe::Unresponsive(error.into()))?;
    if record != json!({"ready": true, "identity": SRT_RELEASE.runner_sha256}) {
        return Err(Probe::Identity);
    }
    Ok(())
}

/// A socket that is not ours or not a socket is never unlinked.
fn remove_stale_socket(path: &Path) -> Result<()> {
    let Ok(info) = fs::symlink_metadata(path) else {
        return Ok(());
    };
    if !info.file_type().is_socket() || info.uid() != current_uid() {
        bail!("Unsafe shared SRT monitor socket");
    }
    fs::remove_file(path)?;
    Ok(())
}

/// `_start_monitor`: a fresh session, a readiness descriptor at the same number, and
/// only PATH in the environment. The probe after `ready` proves the identity.
fn start(
    gobby: &Path,
    node: &Path,
    runner: &Path,
    directory: &Path,
    path: &Path,
    env: &BTreeMap<String, String>,
) -> Result<()> {
    let (mut parent, ready) = UnixStream::pair().context("create monitor readiness pair")?;
    let ready_fd = ready.as_raw_fd();
    let mut command = Command::new(node);
    command
        .arg(runner)
        .arg("--monitor")
        .arg(path)
        .arg(ready_fd.to_string())
        .arg(violations_dir(gobby))
        .env_clear()
        .env("PATH", path_env(env))
        .stdin(Stdio::null())
        .stdout(Stdio::from(open_log(directory)?))
        .stderr(Stdio::from(open_log(directory)?));
    // SAFETY: the closure runs between fork and exec and calls only async-signal-safe
    // libc functions: it starts a new session and clears close-on-exec on the readiness
    // descriptor, which the child needs under the same number.
    unsafe {
        command.pre_exec(move || {
            if libc::setsid() < 0 || libc::fcntl(ready_fd, libc::F_SETFD, 0) < 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = command.spawn().context("start shared SRT monitor")?;
    drop(ready);
    let verified = await_ready(&mut parent)
        .map_err(|error| format!("{error:#}"))
        .and_then(|()| probe(path).map_err(|error| error.to_string()));
    if let Err(error) = verified {
        terminate(&mut child);
        let log = directory.join(LOG_NAME);
        bail!(
            "Shared SRT monitor startup failed ({error}); inspect {}: {}",
            log.display(),
            log_tail(&log)
        );
    }
    // The monitor outlives this launch; reap it whenever it exits so it never
    // lingers as a zombie of the daemon.
    thread::spawn(move || {
        let _ = child.wait();
    });
    Ok(())
}

fn await_ready(parent: &mut UnixStream) -> Result<()> {
    parent.set_read_timeout(Some(READY_TIMEOUT))?;
    let mut line = [0_u8; 6];
    if parent.read_exact(&mut line).is_err() || &line != b"ready\n" {
        bail!("Shared SRT monitor failed to become ready");
    }
    Ok(())
}

/// SIGTERM, then SIGKILL after the bound, as the oracle does.
fn terminate(child: &mut Child) {
    // SAFETY: the pid names a child this process spawned and has not yet reaped.
    unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGTERM) };
    if !matches!(wait_bounded(child, TERMINATE_TIMEOUT), Ok(Some(_))) {
        // A monitor that ignores SIGTERM must not outlive a failed start.
        let _ = child.kill();
        let _ = child.wait();
    }
}

fn log_tail(path: &Path) -> String {
    let text = fs::read(path).unwrap_or_default();
    let start = text.len().saturating_sub(LOG_TAIL_BYTES);
    String::from_utf8_lossy(&text[start..]).into_owned()
}

/// The monitor log opened as `ensure` opens it: append-only, owner-only, never a symlink.
fn open_log(directory: &Path) -> Result<File> {
    OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(directory.join(LOG_NAME))
        .with_context(|| format!("open {}", directory.join(LOG_NAME).display()))
}

fn violations_dir(gobby: &Path) -> PathBuf {
    gobby.join("logs").join("sandbox-violations")
}

fn path_env(env: &BTreeMap<String, String>) -> String {
    env.get("PATH")
        .cloned()
        .unwrap_or_else(|| "/bin:/usr/bin".to_string())
}

fn current_uid() -> u32 {
    // SAFETY: getuid takes no pointers and cannot fail.
    unsafe { libc::getuid() }
}
