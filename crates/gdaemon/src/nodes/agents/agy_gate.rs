//! The agy version gate: the port of `ensure_agy_support`.
use super::executable::find_on_path;
use std::{
    collections::HashMap,
    fs,
    io::Read,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{Mutex, MutexGuard, OnceLock, PoisonError, mpsc},
    thread,
    time::{Duration, Instant},
};

/// The oldest `agy` a node launches.
pub const AGY_REQUIRED_VERSION: &str = "1.1.18";

const PROBE_TIMEOUT: Duration = Duration::from_secs(10);
/// How long stdout may still arrive after the probe child has exited.
const PROBE_DRAIN: Duration = Duration::from_secs(1);
const PROBE_ATTEMPTS: usize = 3;

/// A probed binary: its real path and the metadata a cached record is keyed by.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct Identity {
    path: PathBuf,
    dev: u64,
    ino: u64,
    size: u64,
    mtime: i64,
    mtime_nsec: i64,
}

type Records = HashMap<Identity, Result<(), String>>;

static RECORDS: OnceLock<Mutex<Records>> = OnceLock::new();

/// Probes the `agy` found on `path_env`. `Err` carries the refusal reason.
pub fn ensure_agy_support(path_env: &str, cwd: &Path) -> Result<(), String> {
    let Some(binary) = find_on_path(path_env, "agy") else {
        return Err(refusal("none"));
    };
    for _ in 0..PROBE_ATTEMPTS {
        let Some(before) = identity(&binary) else {
            return Err(refusal("none"));
        };
        if let Some(record) = records().get(&before).cloned() {
            return record;
        }
        let output = probe(&binary, cwd);
        // A binary replaced during its own probe has no trustworthy record; probe the new one.
        if identity(&binary).as_ref() != Some(&before) {
            continue;
        }
        let record = judge(&output);
        records().insert(before, record.clone());
        return record;
    }
    Err(refusal("unparseable"))
}

fn records() -> MutexGuard<'static, Records> {
    RECORDS
        .get_or_init(|| Mutex::new(HashMap::new()))
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
}

fn judge(output: &str) -> Result<(), String> {
    if output.trim().is_empty() {
        return Err(refusal("none"));
    }
    match parse_version(output) {
        Some(version) if version_at_least(version, required()) => Ok(()),
        Some(version) => Err(refusal(&format_version(version))),
        None => Err(refusal("unparseable")),
    }
}

fn refusal(installed: &str) -> String {
    format!(
        "Installed Antigravity version {installed} does not meet required version {AGY_REQUIRED_VERSION}."
    )
}

fn required() -> (u64, u64, u64) {
    parse_triplet(AGY_REQUIRED_VERSION).unwrap_or((0, 0, 0))
}

/// The first `v?MAJOR.MINOR.PATCH` in `output`, as in the Python gate's regex.
fn parse_version(output: &str) -> Option<(u64, u64, u64)> {
    output
        .split(|c: char| !(c.is_ascii_alphanumeric() || c == '.'))
        .find_map(|word| parse_triplet(word.strip_prefix('v').unwrap_or(word)))
}

fn parse_triplet(text: &str) -> Option<(u64, u64, u64)> {
    let mut parts = text.split('.');
    let major = parse_part(parts.next()?)?;
    let minor = parse_part(parts.next()?)?;
    let patch = parse_part(parts.next()?)?;
    parts.next().is_none().then_some((major, minor, patch))
}

fn parse_part(part: &str) -> Option<u64> {
    (!part.is_empty() && part.bytes().all(|b| b.is_ascii_digit()))
        .then(|| part.parse().ok())
        .flatten()
}

fn version_at_least(version: (u64, u64, u64), required: (u64, u64, u64)) -> bool {
    version >= required
}

fn format_version((major, minor, patch): (u64, u64, u64)) -> String {
    format!("{major}.{minor}.{patch}")
}

fn identity(binary: &Path) -> Option<Identity> {
    let path = fs::canonicalize(binary).ok()?;
    let meta = fs::metadata(&path).ok()?;
    Some(Identity {
        path,
        dev: meta.dev(),
        ino: meta.ino(),
        size: meta.size(),
        mtime: meta.mtime(),
        mtime_nsec: meta.mtime_nsec(),
    })
}

/// Runs `binary --version` and returns its stdout. A timeout or a spawn failure reads as empty.
/// Stdout drains on its own thread while the child runs, so a large version output cannot
/// fill the pipe and stall the child into the timeout.
fn probe(binary: &Path, cwd: &Path) -> String {
    let Ok(mut child) = Command::new(binary)
        .arg("--version")
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    else {
        return String::new();
    };
    let (sender, receiver) = mpsc::channel();
    if let Some(mut stdout) = child.stdout.take() {
        thread::spawn(move || {
            let mut output = String::new();
            let _ = stdout.read_to_string(&mut output);
            let _ = sender.send(output);
        });
    }
    let deadline = Instant::now() + PROBE_TIMEOUT;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(10)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return String::new();
            }
        }
    }
    receiver.recv_timeout(PROBE_DRAIN).unwrap_or_default()
}
