//! Unix singleton protocol shared with Python's offline exclusion tooling.
//!
//! Operations are synchronous, bounded local filesystem work. Lifecycle callers
//! must run them outside async executor threads. A claim owns its flock descriptor;
//! it is never handed to a child. Dropping/releasing preserves the PID record, as
//! Python does, and never removes another owner's lock file.

use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::os::fd::{AsRawFd, RawFd};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::time::{SystemTime, UNIX_EPOCH};

use rand::{RngCore, rngs::OsRng};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

pub const SERVICE_LAUNCH_ENV: &str = "GOBBY_SERVICE_LAUNCH";
pub const SERVICE_NONCE_ENV: &str = "GOBBY_SERVICE_NONCE";
const AGE_BOUND: i64 = 30;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Role {
    Daemon,
    Maintenance,
}

impl Role {
    fn name(self) -> &'static str {
        match self {
            Self::Daemon => "daemon",
            Self::Maintenance => "maintenance",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProbeState {
    Absent,
    Daemon,
    Maintenance,
    LiveReservation,
    StaleReservation,
    Transitioning,
}

/// Immutable projection of a service reservation. Nonces are not diagnostic data.
#[derive(Clone, PartialEq)]
pub struct ServiceReservation {
    pub backend: String,
    pub nonce: String,
    pub nonce_path: PathBuf,
    pub issued_at: f64,
    pub boot_id: String,
}

pub struct PidFileClaim {
    file: Option<File>,
    pid_file: PathBuf,
    pub role: Role,
    pub generation: i64,
}

impl PidFileClaim {
    /// Exposed for diagnostics, never for inheritance or ownership transfer.
    pub fn fileno(&self) -> Option<RawFd> {
        self.file.as_ref().map(AsRawFd::as_raw_fd)
    }

    /// Idempotent. Closing the sole owned descriptor releases the flock.
    pub fn release(&mut self) {
        self.file.take();
    }

    /// Publish a service reservation while still holding exclusion, then release.
    /// On failure the claim remains held; nonce cleanup touches only a minted nonce.
    pub fn into_service_reservation(&mut self, backend: &str) -> io::Result<ServiceReservation> {
        let file = self
            .file
            .as_mut()
            .ok_or_else(|| invalid("claim is released"))?;
        let record = clear_stale(read_record(file)?);
        if reservation_is_live(reservation(&record), now(), current_boot_id()) {
            return Err(invalid("a service start reservation is already live"));
        }
        let view = mint_reservation(file, &self.pid_file, backend, next_generation(&record)?)?;
        self.release();
        Ok(view)
    }
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn suffixed(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.as_os_str().to_os_string();
    name.push(suffix);
    PathBuf::from(name)
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

/// The same boot identity used by Python; cached for the life of this process.
pub fn current_boot_id() -> &'static str {
    static BOOT: OnceLock<String> = OnceLock::new();
    BOOT.get_or_init(|| {
        if let Ok(value) = fs::read_to_string("/proc/sys/kernel/random/boot_id") {
            return if value.trim().is_empty() {
                "boot:unknown".into()
            } else {
                value.trim().into()
            };
        }
        #[cfg(target_os = "macos")]
        {
            // sysctlbyname avoids a subprocess and reproduces `sysctl -n kern.boottime`.
            let mut boot = std::mem::MaybeUninit::<libc::timeval>::uninit();
            let mut size = std::mem::size_of::<libc::timeval>();
            // SAFETY: the name is NUL terminated; boot and size point to writable
            // storage of the advertised size. A successful call initializes boot.
            let result = unsafe {
                libc::sysctlbyname(
                    c"kern.boottime".as_ptr(),
                    boot.as_mut_ptr().cast(),
                    &mut size,
                    std::ptr::null_mut(),
                    0,
                )
            };
            if result == 0 && size == std::mem::size_of::<libc::timeval>() {
                // SAFETY: successful sysctl filled the entire timeval above.
                let boot = unsafe { boot.assume_init() };
                return format!(
                    "{{ sec = {}, usec = {} }} {}",
                    boot.tv_sec,
                    boot.tv_usec,
                    boot_time_text(boot.tv_sec)
                );
            }
        }
        "boot:unknown".into()
    })
}

#[cfg(target_os = "macos")]
fn boot_time_text(seconds: libc::time_t) -> String {
    let mut tm = std::mem::MaybeUninit::<libc::tm>::uninit();
    let mut buffer = [0_u8; 64];
    // SAFETY: localtime_r initializes tm on success; strftime writes at most
    // buffer.len() bytes and receives a valid initialized tm and C format.
    let count = unsafe {
        if libc::localtime_r(&seconds, tm.as_mut_ptr()).is_null() {
            return String::new();
        }
        libc::strftime(
            buffer.as_mut_ptr().cast(),
            buffer.len(),
            c"%a %b %e %T %Y".as_ptr(),
            tm.as_ptr(),
        )
    };
    String::from_utf8_lossy(&buffer[..count]).into_owned()
}

fn open_lock(pid_file: &Path) -> io::Result<File> {
    // std sets CLOEXEC atomically on Unix, including descriptors opened by
    // concurrent threads while another thread launches a process.
    OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(suffixed(pid_file, ".lock"))
}

fn try_lock(file: &File) -> io::Result<bool> {
    // SAFETY: the borrowed File keeps the descriptor valid for this syscall.
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
        return Ok(true);
    }
    let error = io::Error::last_os_error();
    if error.kind() == io::ErrorKind::WouldBlock {
        Ok(false)
    } else {
        Err(error)
    }
}

fn read_record(file: &mut File) -> io::Result<Option<Value>> {
    file.seek(SeekFrom::Start(0))?;
    let mut raw = Vec::new();
    file.read_to_end(&mut raw)?;
    Ok(decode_record(&raw))
}

fn write_record(file: &mut File, record: &Value) -> io::Result<()> {
    let raw = encode_record(record)?;
    file.seek(SeekFrom::Start(0))?;
    file.set_len(0)?;
    file.write_all(&raw)?;
    file.sync_all()
}

/// Standalone writers clear their record on failure before dropping the flock.
/// Admission refusals run first so another issuer's reservation stays intact.
/// Held-claim conversion deliberately does not use this failure boundary.
fn acquire_record<T>(
    file: &mut File,
    action: impl FnOnce(&mut File) -> io::Result<T>,
) -> io::Result<T> {
    match action(file) {
        Ok(value) => Ok(value),
        Err(error) => {
            // Match Python's best-effort truncate without masking the failure
            // that caused acquisition to unwind. Dropping File releases flock.
            let _ = file.set_len(0).and_then(|()| file.sync_all());
            Err(error)
        }
    }
}

fn next_generation(record: &Option<Value>) -> io::Result<i64> {
    let generation = record.as_ref().and_then(|r| r.get("generation"));
    let generation = generation
        .and_then(|value| match value {
            Value::Bool(value) => Some(i64::from(*value)),
            Value::String(value) => value.trim().parse().ok(),
            _ => value.as_i64().or_else(|| {
                let number = value.as_f64()?;
                (number.is_finite() && number >= i64::MIN as f64 && number < -(i64::MIN as f64))
                    .then_some(number.trunc() as i64)
            }),
        })
        .unwrap_or(0);
    generation
        .checked_add(1)
        .ok_or_else(|| invalid("generation overflow"))
}

fn role_record(role: &str, generation: i64, ack: Value) -> Value {
    json!({"version":1,"state":role,"role":role,"pid":std::process::id(),
        "boot_id":current_boot_id(),"generation":generation,"reservation":null,"ack":ack})
}

fn write_role(file: &mut File, role: Role, generation: i64, ack: Value) -> io::Result<()> {
    write_record(file, &role_record("transitioning", generation, Value::Null))?;
    write_record(file, &role_record(role.name(), generation, ack))
}

fn write_pid(pid_file: &Path) -> io::Result<()> {
    let mut file = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(pid_file)?;
    file.write_all(std::process::id().to_string().as_bytes())?;
    file.sync_all()
}

fn other_pid_alive(pid_file: &Path) -> bool {
    let Some(pid) = fs::read_to_string(pid_file)
        .ok()
        .and_then(|s| s.trim().parse::<i32>().ok())
    else {
        return false;
    };
    if pid <= 0 || pid as u32 == std::process::id() {
        return false;
    }
    // SAFETY: signal 0 tests existence/permission without delivering a signal.
    unsafe {
        libc::kill(pid, 0) == 0
            || io::Error::last_os_error().kind() == io::ErrorKind::PermissionDenied
    }
}

fn reservation(record: &Option<Value>) -> Option<&Value> {
    record
        .as_ref()?
        .get("reservation")
        .filter(|v| v.is_object())
}

/// Freshness deliberately ignores the issuer PID, which may exit after minting.
pub fn reservation_is_live(payload: Option<&Value>, time: f64, boot: &str) -> bool {
    let Some(payload) = payload else {
        return false;
    };
    if payload.get("boot_id").and_then(Value::as_str) != Some(boot) {
        return false;
    }
    // bool is an int in Python. Numeric strings accept surrounding whitespace.
    let number = |v: &Value| match v {
        Value::Bool(value) => Some(u8::from(*value) as f64),
        Value::String(value) => value.trim().parse().ok(),
        _ => v.as_f64(),
    };
    let issued = match payload.get("issued_at") {
        None => 0.0,
        Some(value) => match number(value) {
            Some(issued) => issued,
            None => return false,
        },
    };
    let bound = match payload.get("age_bound_seconds") {
        None => AGE_BOUND as f64,
        Some(Value::String(s)) => match s.trim().parse::<i64>() {
            Ok(n) => n as f64,
            Err(_) => return false,
        },
        Some(v) => match number(v) {
            Some(n) if n.is_finite() => n.trunc(),
            _ => return false,
        },
    };
    time - issued < bound
}

fn clear_stale(mut record: Option<Value>) -> Option<Value> {
    if let Some(payload) = reservation(&record)
        && !reservation_is_live(Some(payload), now(), current_boot_id())
    {
        if let (Some(path), Some(nonce)) =
            (payload["nonce_path"].as_str(), payload["nonce"].as_str())
        {
            unlink_matching_nonce(Path::new(path), nonce);
        }
        if let Some(record) = &mut record {
            record["reservation"] = Value::Null;
        }
    }
    record
}

pub fn claim_pid_file(pid_file: &Path, role: Role) -> io::Result<Option<PidFileClaim>> {
    let mut file = open_lock(pid_file)?;
    if !try_lock(&file)? {
        return Ok(None);
    }
    let record = clear_stale(read_record(&mut file)?);
    if reservation_is_live(reservation(&record), now(), current_boot_id())
        || other_pid_alive(pid_file)
    {
        return Ok(None);
    }
    let generation = next_generation(&record)?;
    acquire_record(&mut file, |file| {
        write_role(file, role, generation, Value::Null)?;
        if role == Role::Daemon {
            write_pid(pid_file)?;
        }
        Ok(())
    })?;
    Ok(Some(PidFileClaim {
        file: Some(file),
        pid_file: pid_file.into(),
        role,
        generation,
    }))
}

/// Reads a held lock without requiring write permission on its inode.
pub fn probe_daemon_lock(pid_file: &Path) -> io::Result<ProbeState> {
    let mut file = match File::open(suffixed(pid_file, ".lock")) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(ProbeState::Absent),
        Err(error) => return Err(error),
    };
    let held = !try_lock(&file)?;
    let record = read_record(&mut file)?;
    let Some(record) = record else {
        return Ok(if file.metadata()?.len() == 0 && !held {
            ProbeState::Absent
        } else {
            ProbeState::Transitioning
        });
    };
    if record["state"] == "transitioning" || record["role"] == "transitioning" {
        return Ok(ProbeState::Transitioning);
    }
    if let Some(payload) = record.get("reservation").filter(|v| v.is_object()) {
        return Ok(
            if reservation_is_live(Some(payload), now(), current_boot_id()) {
                ProbeState::LiveReservation
            } else {
                ProbeState::StaleReservation
            },
        );
    }
    Ok(match record["role"].as_str() {
        Some("daemon") if held => ProbeState::Daemon,
        Some("maintenance") if held => ProbeState::Maintenance,
        _ if held => ProbeState::Transitioning,
        _ => ProbeState::Absent,
    })
}

fn unlink_matching_nonce(path: &Path, nonce: &str) {
    // Python uses the same owner/mode/content validation as one-shot consume.
    let _ = consume_nonce(path, nonce);
}

fn create_nonce(path: &Path, nonce: &str) -> io::Result<()> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    let result = (|| {
        file.set_permissions(fs::Permissions::from_mode(0o600))?;
        file.write_all(nonce.as_bytes())?;
        file.sync_all()?;
        validate_nonce_owner(&fs::metadata(path)?)
    })();
    if result.is_err() {
        // Only unlink the inode created here, even if the path was replaced.
        if let (Ok(created), Ok(current)) = (file.metadata(), fs::symlink_metadata(path))
            && created.dev() == current.dev()
            && created.ino() == current.ino()
        {
            let _ = fs::remove_file(path);
        }
    }
    result
}

fn validate_nonce_owner(info: &fs::Metadata) -> io::Result<()> {
    // SAFETY: getuid has no arguments or memory preconditions.
    if info.uid() != unsafe { libc::getuid() } || info.mode() & 0o7777 != 0o600 {
        return Err(invalid("nonce file failed owner/mode validation"));
    }
    Ok(())
}

fn consume_nonce(path: &Path, expected: &str) -> io::Result<()> {
    validate_nonce_owner(&fs::metadata(path)?)?;
    if fs::read_to_string(path)? != expected {
        return Err(invalid("nonce content does not match the reservation"));
    }
    fs::remove_file(path)
}

fn mint_reservation(
    file: &mut File,
    pid_file: &Path,
    backend: &str,
    generation: i64,
) -> io::Result<ServiceReservation> {
    let mut bytes = [0_u8; 16];
    OsRng.try_fill_bytes(&mut bytes).map_err(io::Error::other)?;
    let nonce = hex(&bytes);
    let nonce_path = suffixed(pid_file, ".service-nonce");
    let path = nonce_path
        .to_str()
        .ok_or_else(|| invalid("nonce path is not UTF-8"))?;
    let issued_at = now();
    let payload = json!({"backend":backend,"nonce":nonce,"nonce_path":path,
        "issued_at":issued_at,"boot_id":current_boot_id(),"age_bound_seconds":AGE_BOUND});
    write_record(file, &role_record("transitioning", generation, Value::Null))?;
    create_nonce(&nonce_path, &nonce)?;
    let record = json!({"version":1,"state":"reservation","role":null,
        "pid":std::process::id(),"boot_id":current_boot_id(),"generation":generation,
        "reservation":payload,"ack":null});
    if let Err(error) = write_record(file, &record) {
        unlink_matching_nonce(&nonce_path, &nonce);
        return Err(error);
    }
    Ok(ServiceReservation {
        backend: backend.into(),
        nonce,
        nonce_path,
        issued_at,
        boot_id: current_boot_id().into(),
    })
}

pub fn reserve_service_start(pid_file: &Path, backend: &str) -> io::Result<ServiceReservation> {
    let mut file = open_lock(pid_file)?;
    if !try_lock(&file)? {
        return Err(invalid("singleton lock is held"));
    }
    let record = clear_stale(read_record(&mut file)?);
    if reservation_is_live(reservation(&record), now(), current_boot_id()) {
        return Err(invalid("a service start reservation is already live"));
    }
    if other_pid_alive(pid_file) {
        return Err(invalid("a live process still owns the pid file"));
    }
    acquire_record(&mut file, |file| {
        mint_reservation(file, pid_file, backend, next_generation(&record)?)
    })
}

/// Explicit input keeps tests and async callers from mutating process-global env.
pub fn convert_or_acquire_service_claim(
    pid_file: &Path,
    nonce_path: Option<&Path>,
) -> io::Result<PidFileClaim> {
    let mut file = open_lock(pid_file)?;
    if !try_lock(&file)? {
        return Err(invalid("singleton lock is held"));
    }
    let record = clear_stale(read_record(&mut file)?);
    let payload = reservation(&record);
    let live = reservation_is_live(payload, now(), current_boot_id());
    let ack = if live {
        let payload = payload.ok_or_else(|| invalid("live reservation is malformed"))?;
        let expected_path = payload["nonce_path"]
            .as_str()
            .ok_or_else(|| invalid("missing nonce path"))?;
        let path = nonce_path
            .filter(|p| *p == Path::new(expected_path))
            .ok_or_else(|| invalid("service nonce path does not match reservation"))?;
        let expected = payload["nonce"]
            .as_str()
            .ok_or_else(|| invalid("missing nonce"))?;
        consume_nonce(path, expected)?;
        json!({"status":"converted","pid":std::process::id()})
    } else {
        if nonce_path.is_some_and(Path::is_file) {
            return Err(invalid("service nonce replay refused"));
        }
        Value::Null
    };
    let generation = next_generation(&record)?;
    acquire_record(&mut file, |file| {
        write_role(file, Role::Daemon, generation, ack)?;
        write_pid(pid_file)?;
        Ok(())
    })?;
    Ok(PidFileClaim {
        file: Some(file),
        pid_file: pid_file.into(),
        role: Role::Daemon,
        generation,
    })
}

/// Python's marked-service branch selects conversion only for the exact value 1.
pub fn claim_from_environment(pid_file: &Path) -> io::Result<Option<PidFileClaim>> {
    if std::env::var(SERVICE_LAUNCH_ENV).as_deref() == Ok("1") {
        let nonce = std::env::var_os(SERVICE_NONCE_ENV)
            .filter(|s| !s.is_empty())
            .map(PathBuf::from);
        convert_or_acquire_service_claim(pid_file, nonce.as_deref()).map(Some)
    } else {
        claim_pid_file(pid_file, Role::Daemon)
    }
}

pub fn cancel_service_reservation(pid_file: &Path) -> io::Result<ProbeState> {
    let mut file = open_lock(pid_file)?;
    if !try_lock(&file)? {
        return probe_daemon_lock(pid_file);
    }
    if let Some(payload) = reservation(&read_record(&mut file)?)
        && let (Some(path), Some(nonce)) =
            (payload["nonce_path"].as_str(), payload["nonce"].as_str())
    {
        unlink_matching_nonce(Path::new(path), nonce);
    }
    file.set_len(0)?;
    file.sync_all()?;
    Ok(ProbeState::Absent)
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Python's sorted, compact, ensure_ascii=True JSON is the checksum contract.
/// Sorting explicitly avoids depending on serde_json's map feature selection.
fn canonical(value: &Value) -> io::Result<String> {
    Ok(match value {
        Value::Object(map) => {
            let mut keys: Vec<_> = map.keys().collect();
            keys.sort();
            let items = keys
                .into_iter()
                .map(|key| {
                    Ok(format!(
                        "{}:{}",
                        canonical(&Value::String(key.clone()))?,
                        canonical(&map[key])?
                    ))
                })
                .collect::<io::Result<Vec<_>>>()?;
            format!("{{{}}}", items.join(","))
        }
        Value::Array(values) => format!(
            "[{}]",
            values
                .iter()
                .map(canonical)
                .collect::<io::Result<Vec<_>>>()?
                .join(",")
        ),
        Value::String(s) => {
            let quoted = serde_json::to_string(s).map_err(io::Error::other)?;
            let mut ascii = String::new();
            for ch in quoted.chars() {
                if ch >= '\u{7f}' {
                    let mut units = [0_u16; 2];
                    for unit in ch.encode_utf16(&mut units) {
                        ascii.push_str(&format!("\\u{unit:04x}"));
                    }
                } else {
                    ascii.push(ch);
                }
            }
            ascii
        }
        Value::Number(number) if number.is_f64() => {
            python_float(number.as_f64().ok_or_else(|| invalid("invalid float"))?)
        }
        _ => value.to_string(),
    })
}

fn python_float(number: f64) -> String {
    let raw = number.to_string();
    let magnitude = number.abs();
    if magnitude != 0.0 && !(1e-4..1e16).contains(&magnitude) {
        let scientific = format!("{number:e}");
        let (mantissa, exponent) = scientific
            .split_once('e')
            .expect("scientific format contains e");
        let exponent: i32 = exponent.parse().expect("formatted exponent is an integer");
        format!("{mantissa}e{exponent:+03}")
    } else if !raw.contains('.') {
        format!("{raw}.0")
    } else {
        raw
    }
}

pub fn encode_record(record: &Value) -> io::Result<Vec<u8>> {
    let mut body = record
        .as_object()
        .cloned()
        .ok_or_else(|| invalid("record must be an object"))?;
    body.remove("checksum");
    let checksum = hex(&Sha256::digest(
        canonical(&Value::Object(body.clone()))?.as_bytes(),
    ));
    body.insert("checksum".into(), Value::String(checksum));
    Ok(canonical(&Value::Object(body))?.into_bytes())
}

pub fn decode_record(raw: &[u8]) -> Option<Value> {
    let mut value: Value = serde_json::from_slice(raw).ok()?;
    let map = value.as_object_mut()?;
    let checksum = map.remove("checksum")?;
    let expected = hex(&Sha256::digest(canonical(&value).ok()?.as_bytes()));
    if checksum.as_str()? != expected {
        return None;
    }
    value.as_object_mut()?.insert("checksum".into(), checksum);
    Some(value)
}
