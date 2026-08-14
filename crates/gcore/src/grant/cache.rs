//! Grant cache paths, atomic 0600 writes, bindings, and per-file locks.

use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

static TMP_SEQ: AtomicU64 = AtomicU64::new(1);

use serde::{Deserialize, Serialize};

use super::bundle::GrantBundle;
use super::{GrantError, hex_encode, sha256};

const GRANTS_DIR: &str = "grants";
const BINDINGS_DIR: &str = "bindings";
const SETTINGS_SUFFIX: &str = ".settings.json";

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct TrustedBinding {
    pub endpoint: String,
    pub deployment_token: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct CachedSettings {
    pub config_revision: i64,
    pub settings: std::collections::BTreeMap<String, String>,
}

pub fn interactive_cache_path(home: &Path, deployment_token: &str, project_id: &str) -> PathBuf {
    home.join(GRANTS_DIR)
        .join(deployment_token)
        .join(format!("{project_id}.json"))
}

pub fn settings_cache_path(grant_path: &Path) -> PathBuf {
    let mut path = grant_path.as_os_str().to_os_string();
    path.push(SETTINGS_SUFFIX);
    PathBuf::from(path)
}

pub fn binding_path(home: &Path, daemon_url: &str) -> PathBuf {
    let digest = hex_encode(&sha256(normalize_endpoint(daemon_url).as_bytes()));
    home.join(GRANTS_DIR)
        .join(BINDINGS_DIR)
        .join(format!("{}.json", &digest[..16.min(digest.len())]))
}

pub fn grant_lock_path(target: &Path) -> PathBuf {
    let mut path = target.as_os_str().to_os_string();
    path.push(".lock");
    PathBuf::from(path)
}

pub fn normalize_endpoint(url: &str) -> String {
    url.trim().trim_end_matches('/').to_string()
}

pub fn load_binding(home: &Path, daemon_url: &str) -> Option<TrustedBinding> {
    let path = binding_path(home, daemon_url);
    let raw = fs::read(&path).ok()?;
    serde_json::from_slice(&raw).ok()
}

pub fn write_binding(home: &Path, binding: &TrustedBinding) -> Result<(), GrantError> {
    let path = binding_path(home, &binding.endpoint);
    write_json_atomic(&path, &serde_json::to_vec(binding).map_err(json_err)?)
}

pub fn load_grant_file(path: &Path) -> Result<GrantBundle, GrantError> {
    let raw = fs::read(path).map_err(|error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            GrantError::Malformed(format!("grant file missing: {}", path.display()))
        } else {
            GrantError::Io(error.to_string())
        }
    })?;
    super::bundle::parse_grant_json(&raw)
}

pub fn load_settings_file(path: &Path) -> Result<CachedSettings, GrantError> {
    let raw = fs::read(path).map_err(|error| GrantError::Io(error.to_string()))?;
    serde_json::from_slice(&raw).map_err(json_err)
}

pub fn write_grant_file(path: &Path, grant: &GrantBundle) -> Result<(), GrantError> {
    let bytes = grant.model_dump_canonical()?;
    write_bytes_atomic(path, &bytes)
}

pub fn write_settings_file(path: &Path, settings: &CachedSettings) -> Result<(), GrantError> {
    write_json_atomic(path, &serde_json::to_vec(settings).map_err(json_err)?)
}

pub fn write_coherent_pair(
    grant_path: &Path,
    grant: &GrantBundle,
    settings: &CachedSettings,
) -> Result<(), GrantError> {
    if settings.config_revision != grant.config_revision {
        return Err(GrantError::ConfigRevisionMismatch);
    }
    write_grant_file(grant_path, grant)?;
    write_settings_file(&settings_cache_path(grant_path), settings)
}

pub fn load_coherent_pair(
    grant_path: &Path,
) -> Result<Option<(GrantBundle, CachedSettings)>, GrantError> {
    if !grant_path.exists() {
        return Ok(None);
    }
    let grant = load_grant_file(grant_path)?;
    let settings_path = settings_cache_path(grant_path);
    if !settings_path.exists() {
        return Ok(None);
    }
    let settings = load_settings_file(&settings_path)?;
    if settings.config_revision != grant.config_revision {
        return Ok(None);
    }
    Ok(Some((grant, settings)))
}

pub fn newer_generation(existing: Option<&GrantBundle>, incoming: &GrantBundle) -> bool {
    match (
        existing.and_then(GrantBundle::credential_generation),
        incoming.credential_generation(),
    ) {
        (Some(old), Some(new)) => new >= old,
        _ => true,
    }
}

pub struct GrantFileLock {
    path: PathBuf,
}

impl Drop for GrantFileLock {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

pub fn try_lock(path: &Path) -> Result<Option<GrantFileLock>, GrantError> {
    match OpenOptions::new().write(true).create_new(true).open(path) {
        Ok(mut file) => {
            let now = unix_now();
            let body = format!("{}\n{now}\n", std::process::id());
            file.write_all(body.as_bytes())
                .map_err(|error| GrantError::Io(error.to_string()))?;
            let _ = file.sync_all();
            Ok(Some(GrantFileLock {
                path: path.to_path_buf(),
            }))
        }
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => Ok(None),
        Err(error) => Err(GrantError::Io(error.to_string())),
    }
}

pub fn lock_with_deadline(
    path: &Path,
    stale_after: Duration,
    deadline: Instant,
) -> Result<GrantFileLock, GrantError> {
    loop {
        if Instant::now() >= deadline {
            return Err(GrantError::Timeout);
        }
        if let Some(lock) = try_lock(path)? {
            return Ok(lock);
        }
        if lock_is_stale(path, stale_after) {
            let _ = fs::remove_file(path);
            continue;
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        std::thread::sleep(remaining.min(Duration::from_millis(20)));
    }
}

pub fn lock_is_stale(path: &Path, stale_after: Duration) -> bool {
    let Ok(raw) = fs::read_to_string(path) else {
        return true;
    };
    let Some(stamp) = raw.lines().nth(1).and_then(|line| line.parse::<u64>().ok()) else {
        return true;
    };
    let Ok(modified) = SystemTime::now().duration_since(UNIX_EPOCH) else {
        return false;
    };
    modified.as_secs().saturating_sub(stamp) >= stale_after.as_secs()
}

fn write_json_atomic(path: &Path, bytes: &[u8]) -> Result<(), GrantError> {
    write_bytes_atomic(path, bytes)
}

fn write_bytes_atomic(path: &Path, bytes: &[u8]) -> Result<(), GrantError> {
    let parent = path
        .parent()
        .ok_or_else(|| GrantError::Io("grant path has no parent".to_string()))?;
    fs::create_dir_all(parent).map_err(|error| GrantError::Io(error.to_string()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(parent, fs::Permissions::from_mode(0o700));
    }
    let tmp = parent.join(format!(
        ".grant-{}-{}-{}.tmp",
        std::process::id(),
        TMP_SEQ.fetch_add(1, Ordering::Relaxed),
        unix_now()
    ));
    {
        let mut file = File::create(&tmp).map_err(|error| GrantError::Io(error.to_string()))?;
        file.write_all(bytes)
            .map_err(|error| GrantError::Io(error.to_string()))?;
        file.sync_all()
            .map_err(|error| GrantError::Io(error.to_string()))?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&tmp, fs::Permissions::from_mode(0o600))
            .map_err(|error| GrantError::Io(error.to_string()))?;
    }
    fs::rename(&tmp, path).map_err(|error| {
        let _ = fs::remove_file(&tmp);
        GrantError::Io(error.to_string())
    })?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

fn unix_now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0)
}

fn json_err(error: serde_json::Error) -> GrantError {
    GrantError::Malformed(error.to_string())
}
