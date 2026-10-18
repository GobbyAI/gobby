//! Python-compatible filesystem ledger for node-local hook envelopes.
//!
//! Every file operation, including flock and process probing, runs on the blocking
//! pool. Dropping an awaiting future does not interrupt an in-flight atomic mutation.
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{self, Write},
    os::unix::{fs::OpenOptionsExt, fs::PermissionsExt, io::AsRawFd},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};
use time::{Duration, OffsetDateTime, format_description::well_known::Rfc3339};
use uuid::Uuid;

pub const ENVELOPE_REPLAY_GRACE_SECONDS: f64 = 120.0;
pub const ENVELOPE_PROCESSING_LEASE_TTL_SECONDS: f64 = 15.0;
pub const PROCESSED_MARKER_RETENTION_SECONDS: f64 = 86_400.0;
pub const PROCESSED_MARKER_PRUNE_MAX_ENTRIES: usize = 100_000;
pub const STOP_REPLAY_EPOCH_FILENAME: &str = ".stop_replay_epoch";
const OWNER_CREATE_TIME_MATCH_SECONDS: f64 = 1.0;
const PRUNE_ENTRIES_PER_LOCK_HOLD: usize = 64;

fn pass_bound(max: usize) -> usize {
    max.min(PROCESSED_MARKER_PRUNE_MAX_ENTRIES)
}

#[cfg(test)]
mod tests {
    use super::pass_bound;

    #[test]
    fn prune_pass_bound() {
        assert_eq!(pass_bound(usize::MAX), 100_000);
        assert_eq!(pass_bound(2), 2);
    }
}

#[derive(Clone, Debug)]
pub struct Ledger {
    pub inbox: PathBuf,
}

#[derive(Debug, Default)]
pub struct DirectoryPruneResult {
    pub examined: usize,
    pub deleted: usize,
    pub truncated: bool,
}

impl Ledger {
    /// `inbox` is `<gobby_home>/hooks/inbox`; construction performs no I/O.
    pub fn new(inbox: PathBuf) -> Self {
        Self { inbox }
    }

    async fn run<T: Send + 'static>(
        &self,
        work: impl FnOnce(PathBuf) -> io::Result<T> + Send + 'static,
    ) -> io::Result<T> {
        let directory = self.inbox.join("processed");
        tokio::task::spawn_blocking(move || {
            work(directory.clone()).map_err(|error| {
                io::Error::new(
                    error.kind(),
                    format!("hook ledger {}: {error}", directory.display()),
                )
            })
        })
        .await
        .map_err(io::Error::other)?
    }

    pub async fn claim(&self, id: &str) -> io::Result<Option<String>> {
        if id.is_empty() {
            return Ok(None);
        }
        let id = id.to_owned();
        self.run(move |directory| {
            let path = marker_path(&directory, &id);
            let now = OffsetDateTime::now_utc();
            let pid = std::process::id();
            let create_time = process_start(pid).unwrap_or_else(|| unix_seconds(SystemTime::now()));
            let token = Uuid::new_v4().to_string();
            let record = json!({
                "envelope_id":id, "claimed_at":iso(now)?, "status":"processing",
                "owner_token":token, "owner_pid":pid, "owner_create_time":create_time,
                "renewed_at":iso(now)?,
                "lease_expires_at":iso(now + Duration::seconds(ENVELOPE_PROCESSING_LEASE_TTL_SECONDS as i64))?
            });
            let _lock = marker_lock(&path)?;
            let mut file = match OpenOptions::new().write(true).create_new(true).mode(0o600).open(&path) {
                Ok(file) => file,
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => return Ok(None),
                Err(error) => return Err(error),
            };
            file.write_all(&encode(&record)?)?;
            Ok(Some(token))
        }).await
    }

    pub async fn marker(&self, id: &str) -> io::Result<Option<Value>> {
        if id.is_empty() {
            return Ok(None);
        }
        let id = id.to_owned();
        self.run(move |directory| read_marker(&marker_path(&directory, &id), &id))
            .await
    }

    async fn mutate(
        &self,
        id: &str,
        token: &str,
        writer: impl FnOnce(Value) -> io::Result<Option<Value>> + Send + 'static,
    ) -> io::Result<bool> {
        if id.is_empty() || token.is_empty() {
            return Ok(false);
        }
        let (id, token) = (id.to_owned(), token.to_owned());
        self.run(move |directory| {
            let path = marker_path(&directory, &id);
            let _lock = marker_lock(&path)?;
            let Some(record) = read_marker(&path, &id)? else {
                return Ok(false);
            };
            if record["status"] != "processing" || record["owner_token"] != token {
                return Ok(false);
            }
            if let Some(updated) = writer(record)? {
                replace(&path, &encode(&updated)?)?;
            } else {
                fs::remove_file(&path)?;
            }
            Ok(true)
        })
        .await
    }

    pub async fn renew(&self, id: &str, token: &str) -> io::Result<bool> {
        self.mutate(id, token, |mut record| {
            let now = OffsetDateTime::now_utc();
            record["renewed_at"] = json!(iso(now)?);
            record["lease_expires_at"] =
                json!(iso(now
                    + Duration::seconds(
                        ENVELOPE_PROCESSING_LEASE_TTL_SECONDS as i64
                    ))?);
            Ok(Some(record))
        })
        .await
    }

    pub async fn finalize(
        &self,
        id: &str,
        token: &str,
        response: Option<Value>,
        hook: Option<&str>,
    ) -> io::Result<bool> {
        validate_response(&response)?;
        let (envelope, hook) = (id.to_owned(), hook.map(str::to_owned));
        self.mutate(id, token, move |existing| {
            Ok(Some(processed_record(
                &envelope,
                response,
                hook.as_deref(),
                Some(&existing),
            )?))
        })
        .await
    }

    /// Release only the matching owner.
    pub async fn release(&self, id: &str, token: &str) -> io::Result<bool> {
        self.mutate(id, token, |_| Ok(None)).await
    }

    /// A hub requeue has retired this envelope, including any cached response.
    pub async fn forget(&self, id: &str) -> io::Result<()> {
        let id = id.to_owned();
        self.run(move |directory| {
            let path = marker_path(&directory, &id);
            let _lock = marker_lock(&path)?;
            match fs::remove_file(path) {
                Ok(()) => Ok(()),
                Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
                Err(error) => Err(error),
            }
        })
        .await
    }

    /// Recheck the spool and the observed marker under the marker's write lock.
    pub(super) async fn remove_spool_marker(
        &self,
        id: &str,
        expected: Option<Value>,
        processing_only: bool,
        guard: impl FnOnce() -> io::Result<bool> + Send + 'static,
    ) -> io::Result<()> {
        let id = id.to_owned();
        self.run(move |directory| {
            let path = marker_path(&directory, &id);
            let _lock = marker_lock(&path)?;
            let current = read_marker(&path, &id)?;
            if current != expected
                || current.is_none()
                || (processing_only
                    && current
                        .as_ref()
                        .is_none_or(|value| value["status"] != "processing"))
                || !guard()?
            {
                return Ok(());
            }
            fs::remove_file(path)
        })
        .await
    }

    /// Boot invalidates prior process owners even when their leases are fresh.
    pub async fn release_previous_process(&self) -> io::Result<()> {
        self.run(move |directory| {
            let entries = match fs::read_dir(&directory) {
                Ok(entries) => entries,
                Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
                Err(error) => return Err(error),
            };
            let pid = std::process::id();
            let started = process_start(pid);
            for entry in entries {
                let entry = entry?;
                if !entry.file_type()?.is_file()
                    || entry.path().extension().is_none_or(|ext| ext != "json")
                {
                    continue;
                }
                let path = entry.path();
                let _lock = marker_lock(&path)?;
                let Some(record) = read_marker(&path, "")? else {
                    continue;
                };
                let ours = record["owner_pid"].as_u64() == Some(u64::from(pid))
                    && started.is_none_or(|actual| {
                        record["owner_create_time"]
                            .as_f64()
                            .is_some_and(|expected| {
                                (actual - expected).abs() < OWNER_CREATE_TIME_MATCH_SECONDS
                            })
                    });
                if record["status"] == "processing" && !ours {
                    match fs::remove_file(&path) {
                        Ok(()) => {}
                        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                        Err(error) => return Err(error),
                    }
                }
            }
            Ok(())
        })
        .await
    }

    pub async fn is_processed(&self, id: &str) -> io::Result<bool> {
        Ok(self
            .marker(id)
            .await?
            .is_some_and(|record| terminal_status(&record)))
    }

    pub async fn is_active(&self, id: &str, now: Option<OffsetDateTime>) -> io::Result<bool> {
        if id.is_empty() {
            return Ok(false);
        }
        let id = id.to_owned();
        self.run(move |directory| {
            Ok(read_marker(&marker_path(&directory, &id), &id)?
                .is_some_and(|record| active(&record, reference(now))))
        })
        .await
    }

    pub async fn clear_stale(&self, id: &str, now: Option<OffsetDateTime>) -> io::Result<bool> {
        if id.is_empty() {
            return Ok(false);
        }
        let id = id.to_owned();
        self.run(move |directory| {
            let path = marker_path(&directory, &id);
            let _lock = marker_lock(&path)?;
            let now = reference(now);
            let stale = match read_marker(&path, &id)? {
                Some(record) => record["status"] == "processing" && !active(&record, now),
                None => match fs::metadata(&path) {
                    Ok(info) => {
                        seconds(now) - unix_seconds(info.modified()?)
                            >= ENVELOPE_REPLAY_GRACE_SECONDS
                    }
                    Err(error) if error.kind() == io::ErrorKind::NotFound => false,
                    Err(error) => return Err(error),
                },
            };
            if !stale {
                return Ok(false);
            }
            match fs::remove_file(path) {
                Ok(()) => Ok(true),
                Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
                Err(error) => Err(error),
            }
        })
        .await
    }

    /// A hub receipt acknowledgment proves delivery even if a processing lease remains.
    pub async fn mark_acknowledged(&self, id: &str) -> io::Result<()> {
        if id.is_empty() {
            return Ok(());
        }
        let id = id.to_owned();
        self.run(move |directory| {
            let path = marker_path(&directory, &id);
            let _lock = marker_lock(&path)?;
            let existing = read_marker(&path, &id)?;
            if existing.as_ref().is_some_and(terminal_status) {
                return Ok(());
            }
            replace(
                &path,
                &encode(&processed_record(&id, None, None, existing.as_ref())?)?,
            )
        })
        .await
    }

    pub async fn mark_processed(
        &self,
        id: &str,
        response: Option<Value>,
        hook: Option<&str>,
    ) -> io::Result<()> {
        if id.is_empty() {
            return Ok(());
        }
        validate_response(&response)?;
        let (id, hook) = (id.to_owned(), hook.map(str::to_owned));
        self.run(move |directory| {
            let path = marker_path(&directory, &id);
            let _lock = marker_lock(&path)?;
            let existing = read_marker(&path, &id)?;
            if let Some(record) = &existing
                && (record["status"] == "processing"
                    || (response.is_none()
                        && (record.get("status").is_none() || record["status"] == "processed")
                        && record["response"].is_object()))
            {
                return Ok(());
            }
            replace(
                &path,
                &encode(&processed_record(
                    &id,
                    response,
                    hook.as_deref(),
                    existing.as_ref(),
                )?)?,
            )
        })
        .await
    }

    pub async fn terminal_response(
        &self,
        id: &str,
        now: Option<OffsetDateTime>,
    ) -> io::Result<Option<Value>> {
        if id.is_empty() {
            return Ok(None);
        }
        let id = id.to_owned();
        self.run(move |directory| {
            let path = marker_path(&directory, &id);
            let _lock = marker_lock(&path)?;
            let Some(record) = read_marker(&path, &id)? else {
                return Ok(None);
            };
            if !terminal_status(&record) || !record["response"].is_object() {
                return Ok(None);
            }
            let response = &record["response"];
            if stop_block(&record, response) {
                let processed = parse_time(record.get("processed_at"));
                let epoch = read_epoch(&directory);
                let valid = processed.is_some_and(|at| {
                    let age = (reference(now) - at).as_seconds_f64();
                    (0.0..ENVELOPE_REPLAY_GRACE_SECONDS).contains(&age)
                        && epoch.is_none_or(|epoch| at >= epoch)
                });
                if !valid {
                    fs::remove_file(path)?;
                    return Ok(None);
                }
            }
            Ok(Some(response.clone()))
        })
        .await
    }

    pub async fn bump_stop_epoch(&self, now: Option<OffsetDateTime>) -> io::Result<()> {
        self.run(move |directory| {
            fs::create_dir_all(&directory)?;
            replace(
                &directory.join(STOP_REPLAY_EPOCH_FILENAME),
                format!("{}\n", iso(reference(now))?).as_bytes(),
            )
        })
        .await
    }

    pub async fn read_stop_epoch(&self) -> io::Result<Option<OffsetDateTime>> {
        self.run(|directory| Ok(read_epoch(&directory))).await
    }

    /// Fresh regular files do not consume the pass bound; expired and other entries do.
    pub async fn prune(
        &self,
        now: Option<OffsetDateTime>,
        retention: f64,
        max: usize,
    ) -> io::Result<DirectoryPruneResult> {
        self.run(move |directory| {
            Ok(prune(
                &directory,
                seconds(reference(now)) - retention,
                pass_bound(max),
            ))
        })
        .await
    }

    pub fn is_fresh(path: &Path, now: Option<OffsetDateTime>, grace: f64) -> bool {
        let Some(timestamp) = envelope_timestamp_ms_from_inbox_path(path) else {
            return false;
        };
        let Some(created) = timestamp
            .checked_mul(1_000_000)
            .and_then(|ns| OffsetDateTime::from_unix_timestamp_nanos(ns).ok())
        else {
            return false;
        };
        (0.0..grace).contains(&(reference(now) - created).as_seconds_f64())
    }
}

pub fn envelope_timestamp_ms_from_inbox_path(path: &Path) -> Option<i128> {
    let name = path.file_stem()?.to_str()?;
    let mut parts = name.splitn(3, '-');
    if !matches!(parts.next()?, "n" | "c") {
        return None;
    }
    let timestamp = parts.next()?;
    if timestamp.is_empty()
        || !timestamp.bytes().all(|byte| byte.is_ascii_digit())
        || parts.next()?.is_empty()
    {
        return None;
    }
    timestamp.parse().ok()
}

fn marker_path(directory: &Path, id: &str) -> PathBuf {
    directory.join(format!("{:x}.json", Sha256::digest(id.as_bytes())))
}

fn lock_path(marker: &Path) -> PathBuf {
    let name = marker.file_name().unwrap_or_default().as_encoded_bytes();
    let hash = Sha256::digest(name);
    marker
        .parent()
        .unwrap_or(Path::new("."))
        .join(".locks")
        .join(format!(".{:02x}.lock", hash[0]))
}

fn marker_lock(marker: &Path) -> io::Result<File> {
    let path = lock_path(marker);
    fs::create_dir_all(
        path.parent()
            .ok_or_else(|| io::Error::other("lock has no parent"))?,
    )?;
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(path)?;
    file.set_permissions(fs::Permissions::from_mode(0o600))?;
    loop {
        // SAFETY: the descriptor is owned by `file` and remains open through flock.
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) } == 0 {
            return Ok(file);
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}

fn read_marker(path: &Path, id: &str) -> io::Result<Option<Value>> {
    let raw = match fs::read_to_string(path) {
        Ok(raw) => raw,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    Ok(match serde_json::from_str::<Value>(&raw) {
        Ok(record) if record.is_object() => Some(record),
        Ok(_) => None,
        Err(_) if raw.trim().is_empty() => None,
        Err(_) => Some(json!({"envelope_id":id,"status":"processed"})),
    })
}

fn encode(record: &Value) -> io::Result<Vec<u8>> {
    // serde_json's default map is a BTreeMap, matching Python's sort_keys=True.
    let mut bytes = serde_json::to_vec(record).map_err(io::Error::other)?;
    bytes.push(b'\n');
    Ok(bytes)
}

fn replace(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let temporary = path.with_file_name(format!(
        "{}.{}.tmp",
        path.file_name().unwrap_or_default().to_string_lossy(),
        Uuid::new_v4().simple()
    ));
    let result = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)?;
        file.write_all(bytes)?;
        fs::rename(&temporary, path)
    })();
    match fs::remove_file(&temporary) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) if result.is_ok() => return Err(error),
        Err(_) => {}
    }
    result
}

fn validate_response(response: &Option<Value>) -> io::Result<()> {
    if response.as_ref().is_some_and(|value| !value.is_object()) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "hook response must be an object",
        ));
    }
    Ok(())
}

fn processed_record(
    id: &str,
    response: Option<Value>,
    hook: Option<&str>,
    existing: Option<&Value>,
) -> io::Result<Value> {
    let mut record = json!({"envelope_id":id,"processed_at":iso(OffsetDateTime::now_utc())?,"status":"processed"});
    if let Some(response) = response {
        record["response"] = response;
    }
    let hook = hook.filter(|hook| !hook.is_empty()).or_else(|| {
        existing
            .and_then(|record| record["hook_type"].as_str())
            .filter(|hook| !hook.is_empty())
    });
    if let Some(hook) = hook {
        record["hook_type"] = json!(hook);
    }
    Ok(record)
}

fn reference(now: Option<OffsetDateTime>) -> OffsetDateTime {
    now.unwrap_or_else(OffsetDateTime::now_utc)
}
fn seconds(now: OffsetDateTime) -> f64 {
    now.unix_timestamp_nanos() as f64 / 1e9
}
fn unix_seconds(now: SystemTime) -> f64 {
    match now.duration_since(UNIX_EPOCH) {
        Ok(age) => age.as_secs_f64(),
        Err(age) => -age.duration().as_secs_f64(),
    }
}
fn iso(now: OffsetDateTime) -> io::Result<String> {
    now.format(&Rfc3339).map_err(io::Error::other)
}
fn parse_time(value: Option<&Value>) -> Option<OffsetDateTime> {
    let raw = value?.as_str()?;
    let normalized = raw.replace(' ', "T");
    OffsetDateTime::parse(&normalized, &Rfc3339)
        .ok()
        .or_else(|| OffsetDateTime::parse(&format!("{normalized}+00:00"), &Rfc3339).ok())
}
fn read_epoch(directory: &Path) -> Option<OffsetDateTime> {
    let raw = fs::read_to_string(directory.join(STOP_REPLAY_EPOCH_FILENAME)).ok()?;
    parse_time(Some(&json!(raw.trim())))
}
fn terminal_status(record: &Value) -> bool {
    record["status"].is_null() || record["status"] == "processed"
}
fn active(record: &Value, now: OffsetDateTime) -> bool {
    if record["status"] != "processing" {
        return false;
    }
    let live = record["owner_pid"]
        .as_u64()
        .and_then(|pid| u32::try_from(pid).ok())
        .and_then(process_start)
        .zip(record["owner_create_time"].as_f64())
        .is_some_and(|(actual, expected)| {
            (actual - expected).abs() < OWNER_CREATE_TIME_MATCH_SECONDS
        });
    live || if let Some(expiry) = parse_time(record.get("lease_expires_at")) {
        now < expiry
    } else {
        parse_time(record.get("claimed_at")).is_some_and(|at| {
            (0.0..ENVELOPE_REPLAY_GRACE_SECONDS).contains(&(now - at).as_seconds_f64())
        })
    }
}
fn stop_block(record: &Value, response: &Value) -> bool {
    let decision = response["decision"].as_str().unwrap_or("").to_lowercase();
    if !matches!(decision.as_str(), "block" | "deny") {
        return false;
    }
    let hook = record["hook_type"]
        .as_str()
        .unwrap_or("")
        .trim()
        .to_lowercase()
        .replace('-', "_");
    matches!(
        hook.as_str(),
        "stop" | "subagent_stop" | "subagent_end" | "session_end" | "turn_end" | "stop_failure"
    ) || response["reason"]
        .as_str()
        .is_some_and(|reason| reason.contains("[block-terminal-validation-failure]"))
}

#[cfg(target_os = "macos")]
fn process_start(pid: u32) -> Option<f64> {
    let pid = i32::try_from(pid).ok().filter(|pid| *pid > 0)?;
    // SAFETY: proc_bsdinfo is a C POD struct; proc_pidinfo receives its exact size
    // and a writable pointer that lives through the call.
    let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
    let size = std::mem::size_of::<libc::proc_bsdinfo>() as libc::c_int;
    let read = unsafe {
        libc::proc_pidinfo(
            pid,
            libc::PROC_PIDTBSDINFO,
            0,
            (&mut info as *mut libc::proc_bsdinfo).cast(),
            size,
        )
    };
    // Darwin's SZOMB process status is 5 (sys/proc.h).
    (read == size && info.pbi_status != 5)
        .then_some(info.pbi_start_tvsec as f64 + info.pbi_start_tvusec as f64 / 1e6)
}
#[cfg(target_os = "linux")]
fn process_start(pid: u32) -> Option<f64> {
    if pid == 0 {
        return None;
    }
    let stat = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let fields: Vec<_> = stat.rsplit_once(") ")?.1.split_whitespace().collect();
    if *fields.first()? == "Z" {
        return None;
    }
    let ticks: f64 = fields.get(19)?.parse().ok()?;
    let boot = fs::read_to_string("/proc/stat")
        .ok()?
        .lines()
        .find_map(|line| {
            line.strip_prefix("btime ")
                .and_then(|value| value.parse::<f64>().ok())
        })?;
    // SAFETY: sysconf has no pointer arguments or process mutation.
    let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) };
    (hz > 0).then_some(boot + ticks / hz as f64)
}
#[cfg(not(any(target_os = "linux", target_os = "macos")))]
fn process_start(_pid: u32) -> Option<f64> {
    None
}

fn regular_mtime(path: &Path) -> Option<f64> {
    let info = fs::symlink_metadata(path).ok()?;
    info.is_file()
        .then(|| info.modified().ok().map(unix_seconds))
        .flatten()
}
fn prune(directory: &Path, cutoff: f64, max: usize) -> DirectoryPruneResult {
    let mut result = DirectoryPruneResult::default();
    let Ok(entries) = fs::read_dir(directory) else {
        return result;
    };
    let mut counted = 0;
    let mut expired: BTreeMap<PathBuf, Vec<PathBuf>> = BTreeMap::new();
    for entry in entries {
        let Ok(entry) = entry else {
            break;
        };
        let path = entry.path();
        let modified = regular_mtime(&path);
        let stale = modified.is_some_and(|modified| modified < cutoff);
        if modified.is_none() || stale {
            if counted >= max {
                result.truncated = true;
                break;
            }
            counted += 1;
        }
        result.examined += 1;
        if stale {
            expired.entry(lock_path(&path)).or_default().push(path);
        }
    }
    for paths in expired.into_values() {
        for batch in paths.chunks(PRUNE_ENTRIES_PER_LOCK_HOLD) {
            let Ok(_lock) = marker_lock(&batch[0]) else {
                break;
            };
            for path in batch {
                if regular_mtime(path).is_some_and(|modified| modified < cutoff)
                    && fs::remove_file(path).is_ok()
                {
                    result.deleted += 1;
                }
            }
        }
    }
    result
}
