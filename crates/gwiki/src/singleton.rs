use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use fs4::FileExt;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::WikiError;

const RECORD_VERSION: u64 = 1;
const INHERITED_LOCK_FD_ENV: &str = "GOBBY_SINGLETON_LOCK_FD";

static HELD: AtomicBool = AtomicBool::new(false);

pub struct SingletonGuard {
    file: File,
    kind: ClaimKind,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum ClaimKind {
    Acquired,
    Adopted,
}

impl Drop for SingletonGuard {
    fn drop(&mut self) {
        if self.kind == ClaimKind::Acquired {
            let _ = FileExt::unlock(&self.file);
        }
        HELD.store(false, Ordering::SeqCst);
    }
}

pub fn command_is_held() -> bool {
    HELD.load(Ordering::SeqCst)
}

pub fn acquire_or_adopt(mutating: bool) -> Result<Option<SingletonGuard>, WikiError> {
    if !mutating {
        return Ok(None);
    }
    if let Some(guard) = adopt_inherited()? {
        return Ok(Some(guard));
    }
    if command_is_held() {
        return Ok(None);
    }
    match claim_maintenance()? {
        Some(guard) => Ok(Some(guard)),
        None => Err(WikiError::PreconditionFailed {
            detail: "singleton is held; refusing mutating gwiki command".to_string(),
        }),
    }
}

pub fn ensure_maintenance() -> Result<Option<SingletonGuard>, WikiError> {
    if command_is_held() {
        return Ok(None);
    }
    if let Some(guard) = adopt_inherited()? {
        return Ok(Some(guard));
    }
    match claim_maintenance()? {
        Some(guard) => Ok(Some(guard)),
        None => Err(WikiError::PreconditionFailed {
            detail: "singleton is held; refusing registry mutation".to_string(),
        }),
    }
}

fn pid_file() -> Result<PathBuf, WikiError> {
    gobby_core::gobby_home()
        .map(|home| home.join("gobby.pid"))
        .map_err(|error| WikiError::Config {
            detail: format!("cannot resolve Gobby home: {error}"),
        })
}

fn lock_path(pid_file: &Path) -> PathBuf {
    pid_file.with_file_name(format!(
        "{}.lock",
        pid_file
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("gobby.pid")
    ))
}

fn adopt_inherited() -> Result<Option<SingletonGuard>, WikiError> {
    let Ok(raw) = std::env::var(INHERITED_LOCK_FD_ENV) else {
        return Ok(None);
    };
    if raw.trim().is_empty() {
        return Ok(None);
    }
    let fd: i32 = raw.parse().map_err(|_| WikiError::Config {
        detail: format!("invalid {INHERITED_LOCK_FD_ENV}"),
    })?;
    #[cfg(unix)]
    {
        use std::os::unix::io::FromRawFd;
        // SAFETY: parent published this descriptor via GOBBY_SINGLETON_LOCK_FD.
        let file = unsafe { File::from_raw_fd(fd) };
        HELD.store(true, Ordering::SeqCst);
        Ok(Some(SingletonGuard {
            file,
            kind: ClaimKind::Adopted,
        }))
    }
    #[cfg(not(unix))]
    {
        let _ = fd;
        Ok(None)
    }
}

fn claim_maintenance() -> Result<Option<SingletonGuard>, WikiError> {
    let pid_file = pid_file()?;
    if let Some(parent) = pid_file.parent() {
        std::fs::create_dir_all(parent).map_err(|source| WikiError::Io {
            action: "create singleton directory",
            path: Some(parent.to_path_buf()),
            source,
        })?;
    }
    let lock_path = lock_path(&pid_file);
    let mut file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(&lock_path)
        .map_err(|source| WikiError::Io {
            action: "open singleton lock",
            path: Some(lock_path.clone()),
            source,
        })?;
    if FileExt::try_lock(&file).is_err() {
        return Ok(None);
    }
    let existing = read_record(&mut file);
    if reservation_is_live(existing.as_ref()) {
        let _ = FileExt::unlock(&file);
        return Ok(None);
    }
    if let Some(stored_pid) = read_pid(&pid_file)
        && stored_pid != std::process::id()
        && pid_is_alive(stored_pid)
    {
        let _ = FileExt::unlock(&file);
        return Ok(None);
    }
    let generation = next_generation(existing.as_ref());
    write_role_record(&mut file, "maintenance", generation)?;
    write_pid_file(&pid_file)?;
    HELD.store(true, Ordering::SeqCst);
    Ok(Some(SingletonGuard {
        file,
        kind: ClaimKind::Acquired,
    }))
}

fn read_pid(path: &Path) -> Option<u32> {
    let text = std::fs::read_to_string(path).ok()?;
    let pid: u32 = text.trim().parse().ok()?;
    (pid > 0).then_some(pid)
}

fn pid_is_alive(pid: u32) -> bool {
    #[cfg(unix)]
    {
        // SAFETY: kill(pid, 0) only probes existence.
        let result = unsafe { libc::kill(pid as libc::pid_t, 0) };
        result == 0 || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
    }
    #[cfg(not(unix))]
    {
        let _ = pid;
        false
    }
}

fn write_pid_file(path: &Path) -> Result<(), WikiError> {
    std::fs::write(path, format!("{}\n", std::process::id())).map_err(|source| WikiError::Io {
        action: "write singleton pid",
        path: Some(path.to_path_buf()),
        source,
    })
}

fn read_record(file: &mut File) -> Option<Map<String, Value>> {
    file.seek(SeekFrom::Start(0)).ok()?;
    let mut raw = Vec::new();
    file.read_to_end(&mut raw).ok()?;
    decode_record(&raw)
}

fn decode_record(raw: &[u8]) -> Option<Map<String, Value>> {
    let text = std::str::from_utf8(raw).ok()?.trim();
    if text.is_empty() {
        return None;
    }
    let parsed: Value = serde_json::from_str(text).ok()?;
    let object = parsed.as_object()?.clone();
    let checksum = object.get("checksum")?.as_str()?.to_string();
    let mut body = object.clone();
    body.remove("checksum");
    if checksum != checksum_payload(&body) {
        return None;
    }
    Some(object)
}

fn checksum_payload(body: &Map<String, Value>) -> String {
    let canonical = serde_json::to_vec(&Value::Object(body.clone())).unwrap_or_default();
    hex_sha256(&canonical)
}

fn hex_sha256(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn next_generation(record: Option<&Map<String, Value>>) -> u64 {
    record
        .and_then(|record| record.get("generation"))
        .and_then(Value::as_u64)
        .map_or(1, |generation| generation + 1)
}

fn reservation_is_live(record: Option<&Map<String, Value>>) -> bool {
    let Some(reservation) = record.and_then(|record| record.get("reservation")) else {
        return false;
    };
    if reservation.is_null() {
        return false;
    }
    true
}

fn write_role_record(file: &mut File, role: &str, generation: u64) -> Result<(), WikiError> {
    let mut record = Map::new();
    record.insert("ack".into(), Value::Null);
    record.insert("boot_id".into(), Value::String(current_boot_id()));
    record.insert("generation".into(), Value::from(generation));
    record.insert("pid".into(), Value::from(std::process::id()));
    record.insert("reservation".into(), Value::Null);
    record.insert("role".into(), Value::String(role.to_string()));
    record.insert("state".into(), Value::String(role.to_string()));
    record.insert("version".into(), Value::from(RECORD_VERSION));
    let checksum = checksum_payload(&record);
    record.insert("checksum".into(), Value::String(checksum));
    let payload = serde_json::to_vec(&Value::Object(record)).map_err(|source| WikiError::Json {
        action: "serialize singleton record",
        path: None,
        source,
    })?;
    file.seek(SeekFrom::Start(0))
        .map_err(|source| WikiError::Io {
            action: "seek singleton record",
            path: None,
            source,
        })?;
    file.set_len(0).map_err(|source| WikiError::Io {
        action: "truncate singleton record",
        path: None,
        source,
    })?;
    file.write_all(&payload).map_err(|source| WikiError::Io {
        action: "write singleton record",
        path: None,
        source,
    })?;
    file.sync_all().map_err(|source| WikiError::Io {
        action: "sync singleton record",
        path: None,
        source,
    })
}

fn current_boot_id() -> String {
    #[cfg(target_os = "macos")]
    {
        if let Ok(output) = std::process::Command::new("sysctl")
            .args(["-n", "kern.boottime"])
            .output()
            && output.status.success()
        {
            let text = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if !text.is_empty() {
                return text;
            }
        }
    }
    "boot:unknown".to_string()
}
