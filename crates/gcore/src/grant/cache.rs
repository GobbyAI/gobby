//! Grant cache paths, atomic 0600 writes, bindings, and per-file locks.

use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

static TMP_SEQ: AtomicU64 = AtomicU64::new(1);

use serde::{Deserialize, Serialize};

use super::bundle::{GrantBundle, parse_grant_json};
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

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct CacheEnvelope {
    grant: GrantBundle,
    settings: CachedSettings,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CachePair {
    Coherent(GrantBundle, CachedSettings),
    GrantOnly(GrantBundle),
    Incoherent(GrantBundle),
}

/// Cache file for one interactive grant. A worktree or clone caches under its own
/// overlay-qualified name so the main checkout and its isolation workspaces never
/// share (or clobber) a grant bound to a different overlay.
pub fn interactive_cache_path(
    home: &Path,
    deployment_token: &str,
    project_id: &str,
    code_overlay_project_id: Option<&str>,
) -> PathBuf {
    let file_name = match code_overlay_project_id {
        Some(overlay) => format!("{project_id}--{overlay}.json"),
        None => format!("{project_id}.json"),
    };
    home.join(GRANTS_DIR).join(deployment_token).join(file_name)
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
    let raw = read_cache_bytes(path)?;
    parse_cache_bytes(&raw).map(|(grant, _)| grant)
}

pub(crate) fn is_missing_grant_file(error: &GrantError) -> bool {
    matches!(error, GrantError::Malformed(message) if message.starts_with("grant file missing:"))
}

pub fn load_settings_file(path: &Path) -> Result<CachedSettings, GrantError> {
    let raw = fs::read(path).map_err(|error| GrantError::Io(error.to_string()))?;
    serde_json::from_slice(&raw).map_err(json_err)
}

pub fn write_grant_file(path: &Path, grant: &GrantBundle) -> Result<(), GrantError> {
    let bytes = grant.model_dump_canonical()?;
    write_bytes_atomic(path, &bytes)?;
    let _ = fs::remove_file(settings_cache_path(path));
    Ok(())
}

pub fn write_coherent_pair(
    grant_path: &Path,
    grant: &GrantBundle,
    settings: &CachedSettings,
) -> Result<(), GrantError> {
    if settings.config_revision != grant.config_revision {
        return Err(GrantError::ConfigRevisionMismatch);
    }
    let envelope = CacheEnvelope {
        grant: grant.clone(),
        settings: settings.clone(),
    };
    write_json_atomic(
        grant_path,
        &serde_json::to_vec(&envelope).map_err(json_err)?,
    )?;
    let _ = fs::remove_file(settings_cache_path(grant_path));
    Ok(())
}

pub fn persist_cache(
    grant_path: &Path,
    grant: &GrantBundle,
    settings: Option<&CachedSettings>,
) -> Result<(), GrantError> {
    match settings {
        Some(settings) => write_coherent_pair(grant_path, grant, settings),
        None => write_grant_file(grant_path, grant),
    }
}

/// Best-effort removal of a cache pair that can no longer be trusted.
pub fn discard_cache_pair(grant_path: &Path) {
    let _ = fs::remove_file(grant_path);
    let _ = fs::remove_file(settings_cache_path(grant_path));
}

pub fn matching_settings(path: &Path, grant: &GrantBundle) -> Option<CachedSettings> {
    match inspect_cache_pair(path) {
        Ok(Some(CachePair::Coherent(cached, settings)))
            if cached.config_revision == grant.config_revision =>
        {
            Some(settings)
        }
        _ => None,
    }
}

pub fn inspect_cache_pair(grant_path: &Path) -> Result<Option<CachePair>, GrantError> {
    if !grant_path.exists() {
        return Ok(None);
    }
    let raw = read_cache_bytes(grant_path)?;
    let (grant, envelope_settings) = parse_cache_bytes(&raw)?;
    if let Some(settings) = envelope_settings {
        if settings.config_revision == grant.config_revision {
            return Ok(Some(CachePair::Coherent(grant, settings)));
        }
        return Ok(Some(CachePair::Incoherent(grant)));
    }
    let settings_path = settings_cache_path(grant_path);
    if !settings_path.exists() {
        return Ok(Some(CachePair::GrantOnly(grant)));
    }
    let settings = load_settings_file(&settings_path)?;
    if settings.config_revision == grant.config_revision {
        Ok(Some(CachePair::Coherent(grant, settings)))
    } else {
        Ok(Some(CachePair::Incoherent(grant)))
    }
}

fn read_cache_bytes(path: &Path) -> Result<Vec<u8>, GrantError> {
    fs::read(path).map_err(|error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            GrantError::Malformed(format!("grant file missing: {}", path.display()))
        } else {
            GrantError::Io(error.to_string())
        }
    })
}

fn parse_cache_bytes(raw: &[u8]) -> Result<(GrantBundle, Option<CachedSettings>), GrantError> {
    let trimmed = raw.trim_ascii();
    let Ok(value) = serde_json::from_slice::<serde_json::Value>(trimmed) else {
        return Ok((parse_grant_json(raw)?, None));
    };
    if let Some(grant_value) = value.get("grant") {
        let grant_raw = serde_json::to_vec(grant_value).map_err(json_err)?;
        let grant = parse_grant_json(&grant_raw)?;
        let settings = match value.get("settings") {
            Some(settings) => Some(serde_json::from_value(settings.clone()).map_err(json_err)?),
            None => None,
        };
        return Ok((grant, settings));
    }
    Ok((parse_grant_json(raw)?, None))
}

pub fn newer_generation(existing: Option<&GrantBundle>, incoming: &GrantBundle) -> bool {
    match (
        existing.and_then(GrantBundle::credential_generation),
        incoming.credential_generation(),
    ) {
        (Some(old), Some(new)) => new >= old,
        _ => existing.is_none_or(|grant| {
            incoming.deployment.fencing_epoch >= grant.deployment.fencing_epoch
        }),
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
        fs::set_permissions(parent, fs::Permissions::from_mode(0o700))
            .map_err(|error| GrantError::Io(error.to_string()))?;
    }
    let tmp = parent.join(format!(
        ".grant-{}-{}-{}.tmp",
        std::process::id(),
        TMP_SEQ.fetch_add(1, Ordering::Relaxed),
        unix_now()
    ));
    {
        let mut file = create_temp_file(&tmp)?;
        file.write_all(bytes)
            .map_err(|error| GrantError::Io(error.to_string()))?;
        file.sync_all()
            .map_err(|error| GrantError::Io(error.to_string()))?;
    }
    fs::rename(&tmp, path).map_err(|error| {
        let _ = fs::remove_file(&tmp);
        GrantError::Io(error.to_string())
    })?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))
            .map_err(|error| GrantError::Io(error.to_string()))?;
    }
    Ok(())
}

#[cfg(unix)]
fn create_temp_file(path: &Path) -> Result<File, GrantError> {
    use std::os::unix::fs::OpenOptionsExt;
    OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
        .map_err(|error| GrantError::Io(error.to_string()))
}

#[cfg(not(unix))]
fn create_temp_file(path: &Path) -> Result<File, GrantError> {
    File::create(path).map_err(|error| GrantError::Io(error.to_string()))
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
