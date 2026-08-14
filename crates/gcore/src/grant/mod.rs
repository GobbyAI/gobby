//! gcore grant client: handshake, cache, renewal, and typed errors.

mod bundle;
mod cache;
mod handshake;

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use thiserror::Error;

pub use bundle::{
    AiCapability, BrokerOperation, EXPECTED_API_CONTRACT, FalkorCapability, GRANT_VERSION,
    GrantBundle, GrantCapabilities, GrantDeployment, GrantPrincipal, GrantSchemaIdentity,
    PostgresCapability, PrincipalKind, QdrantCapability, canonical_payload_bytes,
    expected_schema_identity, parse_grant_json, payload_checksum, validate_for_construction,
    verify_payload_checksum,
};
pub use cache::{
    CachedSettings, GrantFileLock, TrustedBinding, binding_path, grant_lock_path,
    interactive_cache_path, load_binding, load_grant_file, settings_cache_path, try_lock,
    write_binding, write_coherent_pair, write_grant_file,
};
pub use handshake::{
    CapabilityClaims, GRANT_HEADER, MANAGED_BOOTSTRAP_ENV, daemon_reachable, encode_grant_header,
    parse_capability_token, reject_remote_endpoint,
};

use bundle::validate_for_construction as validate_grant;
use cache::{
    load_coherent_pair, load_settings_file, lock_with_deadline, newer_generation,
    write_settings_file,
};
use handshake::{
    challenge_and_handshake, deployment_token as derived_deployment_token,
    is_default_local_endpoint, parse_capability_token as parse_envelope,
};

const DEFAULT_DEADLINE: Duration = Duration::from_secs(5);
const DEFAULT_STALE_LOCK: Duration = Duration::from_secs(10);
const REACHABILITY_PROBE: Duration = Duration::from_millis(150);

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum GrantError {
    #[error("daemon required")]
    DaemonRequired,
    #[error("grant expired")]
    Expired,
    #[error("schema identity mismatch")]
    SchemaMismatch,
    #[error("deployment mismatch")]
    DeploymentMismatch,
    #[error("api contract mismatch")]
    ApiContractMismatch,
    #[error("remote daemon endpoint refused")]
    RemoteEndpoint,
    #[error("config revision mismatch")]
    ConfigRevisionMismatch,
    #[error("grant revoked")]
    Revoked,
    #[error("grant operation timed out")]
    Timeout,
    #[error("malformed grant: {0}")]
    Malformed(String),
    #[error("grant io error: {0}")]
    Io(String),
}

impl GrantError {
    pub fn cli_code(&self) -> &'static str {
        match self {
            Self::DaemonRequired => "daemon_required",
            Self::Expired => "expired",
            Self::SchemaMismatch => "schema_mismatch",
            Self::DeploymentMismatch => "deployment_mismatch",
            Self::ApiContractMismatch => "api_contract_mismatch",
            Self::RemoteEndpoint => "remote_endpoint",
            Self::ConfigRevisionMismatch => "config_revision_mismatch",
            Self::Revoked => "revoked",
            Self::Timeout => "timeout",
            Self::Malformed(_) => "malformed",
            Self::Io(_) => "io",
        }
    }

    pub fn exit_status(&self) -> i32 {
        match self {
            Self::Io(_) => 1,
            _ => 2,
        }
    }

    fn is_stale_epoch(&self) -> bool {
        matches!(self, Self::Malformed(message) if message == "stale_epoch")
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GrantSource {
    ManagedFile,
    Cache,
    Handshake,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AcquiredGrant {
    pub bundle: GrantBundle,
    pub source: GrantSource,
    pub settings: Option<CachedSettings>,
    pub daemon_reachable: bool,
    now: i64,
}

impl AcquiredGrant {
    pub fn permits_datastore(&self) -> bool {
        !self.bundle.is_expired(self.now)
    }

    pub fn permits_ai(&self) -> bool {
        self.permits_datastore() && self.daemon_reachable
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CachedGrantInspection {
    Absent,
    Malformed,
    Valid { expires_at: i64, remaining_ttl: i64 },
    Expiring { expires_at: i64, remaining_ttl: i64 },
    Expired { expires_at: i64 },
}

#[derive(Clone, Debug)]
pub struct AcquireRequest<'a> {
    pub project_root: &'a Path,
    pub home: Option<&'a Path>,
    pub daemon_url: Option<String>,
    pub now: Option<i64>,
    pub session_id: Option<String>,
    pub deadline: Option<Duration>,
    pub stale_lock_after: Option<Duration>,
}

impl<'a> AcquireRequest<'a> {
    pub fn new(project_root: &'a Path) -> Self {
        Self {
            project_root,
            home: None,
            daemon_url: None,
            now: None,
            session_id: None,
            deadline: None,
            stale_lock_after: None,
        }
    }
}

#[derive(Clone, Debug)]
struct AcquireCtx {
    home: PathBuf,
    project_id: String,
    machine_id: String,
    daemon_url: String,
    now: i64,
    session_id: Option<String>,
    deadline: Instant,
    stale_lock_after: Duration,
}

pub fn acquire(project_root: impl AsRef<Path>) -> Result<AcquiredGrant, GrantError> {
    acquire_with(&AcquireRequest::new(project_root.as_ref()))
}

pub fn acquire_with(request: &AcquireRequest<'_>) -> Result<AcquiredGrant, GrantError> {
    let ctx = AcquireCtx::from_request(request)?;
    handshake::reject_remote_endpoint(&ctx.daemon_url)?;
    if let Some(path) = managed_bootstrap_path() {
        return acquire_managed(&ctx, &path);
    }
    acquire_interactive(&ctx)
}

pub fn inspect_cached_grant(project_root: impl AsRef<Path>) -> CachedGrantInspection {
    inspect_cached_grant_at(project_root.as_ref(), None, None, None)
}

pub fn inspect_cached_grant_at(
    project_root: &Path,
    home: Option<&Path>,
    daemon_url: Option<&str>,
    now: Option<i64>,
) -> CachedGrantInspection {
    let Ok(home) = resolve_home(home) else {
        return CachedGrantInspection::Malformed;
    };
    let Ok(project_id) = crate::project::read_project_id(project_root) else {
        return CachedGrantInspection::Malformed;
    };
    let now = now.unwrap_or_else(unix_now);
    let daemon_url = daemon_url
        .map(ToOwned::to_owned)
        .unwrap_or_else(crate::daemon_url::daemon_url);
    let token = load_binding(&home, &daemon_url)
        .map(|binding| binding.deployment_token)
        .or_else(|| {
            is_default_local_endpoint(&daemon_url)
                .ok()
                .filter(|is_default| *is_default)
                .map(|_| derived_deployment_token(&home))
        });
    let Some(token) = token else {
        return CachedGrantInspection::Absent;
    };
    let path = interactive_cache_path(&home, &token, &project_id);
    if !path.exists() {
        return CachedGrantInspection::Absent;
    }
    let Ok(grant) = load_grant_file(&path) else {
        return CachedGrantInspection::Malformed;
    };
    if grant.is_expired(now) {
        return CachedGrantInspection::Expired {
            expires_at: grant.expires_at,
        };
    }
    if grant.past_half_ttl(now) {
        return CachedGrantInspection::Expiring {
            expires_at: grant.expires_at,
            remaining_ttl: grant.remaining_ttl(now),
        };
    }
    CachedGrantInspection::Valid {
        expires_at: grant.expires_at,
        remaining_ttl: grant.remaining_ttl(now),
    }
}

pub fn deployment_token(data_root: &Path) -> String {
    derived_deployment_token(data_root)
}

pub fn fetch_runtime_config(
    base_url: &str,
    grant: &GrantBundle,
    bearer: Option<&str>,
    timeout: Duration,
) -> Result<CachedSettings, GrantError> {
    let url = format!(
        "{}{}",
        cache::normalize_endpoint(base_url),
        "/api/runtime/config"
    );
    let response = handshake::http_json("GET", &url, None, bearer, Some(grant), timeout)?;
    if !(200..300).contains(&response.status) {
        if response.status == 403 && response.body.contains("revoked") {
            return Err(GrantError::Revoked);
        }
        if response.status == 401 {
            return Err(GrantError::Expired);
        }
        return Err(GrantError::Malformed(format!(
            "runtime config failed with HTTP {}",
            response.status
        )));
    }
    #[derive(serde::Deserialize)]
    struct Envelope {
        config_revision: i64,
        settings: std::collections::BTreeMap<String, String>,
    }
    let envelope: Envelope = serde_json::from_str(&response.body)
        .map_err(|error| GrantError::Malformed(error.to_string()))?;
    Ok(CachedSettings {
        config_revision: envelope.config_revision,
        settings: envelope.settings,
    })
}

impl AcquireCtx {
    fn from_request(request: &AcquireRequest<'_>) -> Result<Self, GrantError> {
        let home = resolve_home(request.home)?;
        let project_id = crate::project::read_project_id(request.project_root)
            .map_err(|error| GrantError::Malformed(error.to_string()))?;
        let machine_id = crate::machine::read_machine_id_from_home(&home)
            .map_err(|error| GrantError::Malformed(error.to_string()))?;
        Ok(Self {
            home,
            project_id,
            machine_id,
            daemon_url: request
                .daemon_url
                .clone()
                .unwrap_or_else(crate::daemon_url::daemon_url),
            now: request.now.unwrap_or_else(unix_now),
            session_id: request.session_id.clone(),
            deadline: Instant::now() + request.deadline.unwrap_or(DEFAULT_DEADLINE),
            stale_lock_after: request.stale_lock_after.unwrap_or(DEFAULT_STALE_LOCK),
        })
    }

    fn reachable(&self) -> bool {
        Instant::now() < self.deadline
            && daemon_reachable(
                &self.daemon_url,
                REACHABILITY_PROBE.min(self.deadline.saturating_duration_since(Instant::now())),
            )
    }

    fn remaining(&self) -> Result<Duration, GrantError> {
        let remaining = self.deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            Err(GrantError::Timeout)
        } else {
            Ok(remaining)
        }
    }
}

fn acquire_managed(ctx: &AcquireCtx, path: &Path) -> Result<AcquiredGrant, GrantError> {
    let grant = load_grant_file(path)?;
    validate_grant(
        &grant,
        &ctx.project_id,
        &ctx.machine_id,
        Some(&grant.deployment.token),
        true,
    )?;
    validate_managed_identity(&grant)?;
    let destination = path.to_path_buf();
    finish_loaded(ctx, grant, GrantSource::ManagedFile, destination, true)
}

fn acquire_interactive(ctx: &AcquireCtx) -> Result<AcquiredGrant, GrantError> {
    let binding = load_binding(&ctx.home, &ctx.daemon_url);
    if let Some(binding) = binding {
        let path = interactive_cache_path(&ctx.home, &binding.deployment_token, &ctx.project_id);
        if let Some((grant, settings)) = load_coherent_pair(&path)? {
            validate_grant(
                &grant,
                &ctx.project_id,
                &ctx.machine_id,
                Some(&binding.deployment_token),
                false,
            )?;
            return finish_loaded_with_settings(
                ctx,
                grant,
                settings,
                GrantSource::Cache,
                path,
                false,
            );
        }
        if path.exists() {
            let grant = load_grant_file(&path)?;
            validate_grant(
                &grant,
                &ctx.project_id,
                &ctx.machine_id,
                Some(&binding.deployment_token),
                false,
            )?;
            return finish_loaded(ctx, grant, GrantSource::Cache, path, false);
        }
        if ctx.reachable() {
            return handshake_interactive(ctx, Some(&binding.deployment_token), false);
        }
        return Err(GrantError::DaemonRequired);
    }

    if is_default_local_endpoint(&ctx.daemon_url).unwrap_or(false) && ctx.reachable() {
        return handshake_interactive(ctx, Some(&derived_deployment_token(&ctx.home)), true);
    }
    if ctx.reachable() {
        return handshake_interactive(ctx, None, false);
    }
    Err(GrantError::DaemonRequired)
}

fn finish_loaded(
    ctx: &AcquireCtx,
    grant: GrantBundle,
    source: GrantSource,
    destination: PathBuf,
    managed: bool,
) -> Result<AcquiredGrant, GrantError> {
    if grant.is_expired(ctx.now) {
        return refresh_or_fail(ctx, Some(&grant), source, destination, managed, true);
    }
    if grant.past_half_ttl(ctx.now) && ctx.reachable() {
        let lock_path = grant_lock_path(&destination);
        if let Some(_lock) = try_lock(&lock_path)? {
            let refreshed = refresh_or_fail(
                ctx,
                Some(&grant),
                source,
                destination.clone(),
                managed,
                false,
            );
            if let Ok(acquired) = refreshed {
                return Ok(acquired);
            }
        }
    }
    let reachable = ctx.reachable();
    let settings = load_settings_file(&settings_cache_path(&destination)).ok();
    Ok(AcquiredGrant {
        bundle: grant,
        source,
        settings,
        daemon_reachable: reachable,
        now: ctx.now,
    })
}

fn finish_loaded_with_settings(
    ctx: &AcquireCtx,
    grant: GrantBundle,
    settings: CachedSettings,
    source: GrantSource,
    destination: PathBuf,
    managed: bool,
) -> Result<AcquiredGrant, GrantError> {
    if grant.is_expired(ctx.now) {
        return refresh_or_fail(ctx, Some(&grant), source, destination, managed, true);
    }
    if grant.past_half_ttl(ctx.now) && ctx.reachable() {
        let lock_path = grant_lock_path(&destination);
        if let Some(_lock) = try_lock(&lock_path)? {
            let refreshed = refresh_or_fail(
                ctx,
                Some(&grant),
                source,
                destination.clone(),
                managed,
                false,
            );
            if let Ok(acquired) = refreshed {
                return Ok(acquired);
            }
        }
    }
    Ok(AcquiredGrant {
        bundle: grant,
        source,
        settings: Some(settings),
        daemon_reachable: ctx.reachable(),
        now: ctx.now,
    })
}

fn refresh_or_fail(
    ctx: &AcquireCtx,
    existing: Option<&GrantBundle>,
    source: GrantSource,
    destination: PathBuf,
    managed: bool,
    mandatory: bool,
) -> Result<AcquiredGrant, GrantError> {
    if managed {
        let _ = managed_envelope(ctx, existing)?;
        // Envelope validity is independent of daemon reachability so a missing
        // launch token never falls through to a blocking dial.
    }
    if !ctx.reachable() {
        return if mandatory {
            Err(GrantError::DaemonRequired)
        } else {
            let grant = existing.cloned().ok_or(GrantError::DaemonRequired)?;
            Ok(AcquiredGrant {
                bundle: grant,
                source,
                settings: load_settings_file(&settings_cache_path(&destination)).ok(),
                daemon_reachable: false,
                now: ctx.now,
            })
        };
    }
    let lock_path = grant_lock_path(&destination);
    let _lock = if mandatory {
        lock_with_deadline(&lock_path, ctx.stale_lock_after, ctx.deadline)?
    } else {
        match try_lock(&lock_path)? {
            Some(lock) => lock,
            None => {
                let grant = existing.cloned().ok_or(GrantError::DaemonRequired)?;
                return Ok(AcquiredGrant {
                    bundle: grant,
                    source,
                    settings: load_settings_file(&settings_cache_path(&destination)).ok(),
                    daemon_reachable: true,
                    now: ctx.now,
                });
            }
        }
    };
    if let Ok(current) = load_grant_file(&destination)
        && !current.is_expired(ctx.now)
        && !current.past_half_ttl(ctx.now)
    {
        return Ok(AcquiredGrant {
            bundle: current,
            source,
            settings: load_settings_file(&settings_cache_path(&destination)).ok(),
            daemon_reachable: true,
            now: ctx.now,
        });
    }
    if managed {
        handshake_managed(ctx, existing, destination)
    } else {
        handshake_interactive(
            ctx,
            existing.map(|grant| grant.deployment.token.as_str()),
            false,
        )
        .or_else(|error| {
            if error.is_stale_epoch() {
                handshake_interactive(
                    ctx,
                    existing.map(|grant| grant.deployment.token.as_str()),
                    false,
                )
            } else {
                Err(error)
            }
        })
    }
}

fn handshake_interactive(
    ctx: &AcquireCtx,
    expected_deployment: Option<&str>,
    verify_derived: bool,
) -> Result<AcquiredGrant, GrantError> {
    let token = interactive_bearer(&ctx.home)?;
    let expected = if verify_derived {
        Some(derived_deployment_token(&ctx.home))
    } else {
        expected_deployment.map(ToOwned::to_owned)
    };
    let grant = challenge_and_handshake(
        &ctx.daemon_url,
        &token,
        &ctx.machine_id,
        &ctx.project_id,
        ctx.session_id.as_deref().or(Some("cli")),
        None,
        ctx.deadline,
    )?;
    let expected_ref = expected.as_deref();
    validate_grant(
        &grant,
        &ctx.project_id,
        &ctx.machine_id,
        expected_ref,
        false,
    )?;
    persist_interactive(ctx, grant, &token)
}

fn managed_envelope(
    ctx: &AcquireCtx,
    existing: Option<&GrantBundle>,
) -> Result<(String, CapabilityClaims), GrantError> {
    let envelope =
        std::env::var(crate::local_token::AGENT_API_TOKEN_ENV).map_err(|_| GrantError::Expired)?;
    let envelope = envelope.trim();
    if envelope.is_empty() {
        return Err(GrantError::Expired);
    }
    let claims = parse_envelope(envelope)?;
    if claims.exp <= ctx.now {
        return Err(GrantError::Expired);
    }
    if let Some(existing) = existing
        && !claims.matches_principal(&existing.principal)
    {
        return Err(GrantError::Malformed(
            "envelope token principal mismatch".to_string(),
        ));
    }
    Ok((envelope.to_string(), claims))
}

fn handshake_managed(
    ctx: &AcquireCtx,
    existing: Option<&GrantBundle>,
    destination: PathBuf,
) -> Result<AcquiredGrant, GrantError> {
    let (envelope, claims) = managed_envelope(ctx, existing)?;
    let grant = challenge_and_handshake(
        &ctx.daemon_url,
        &envelope,
        &ctx.machine_id,
        &ctx.project_id,
        Some(&claims.session_id),
        Some(&claims),
        ctx.deadline,
    )?;
    validate_grant(
        &grant,
        &ctx.project_id,
        &ctx.machine_id,
        existing.map(|grant| grant.deployment.token.as_str()),
        true,
    )?;
    if !newer_generation(existing, &grant) {
        let existing = existing.cloned().ok_or(GrantError::Malformed(
            "managed refresh refused a generation downgrade".to_string(),
        ))?;
        return Ok(AcquiredGrant {
            bundle: existing,
            source: GrantSource::ManagedFile,
            settings: None,
            daemon_reachable: true,
            now: ctx.now,
        });
    }
    let (grant, settings) =
        fetch_settings_coherent(ctx, grant, Some(&envelope), existing.cloned(), true)?;
    write_grant_file(&destination, &grant)?;
    if let Some(settings) = &settings {
        write_settings_file(&settings_cache_path(&destination), settings)?;
    }
    Ok(AcquiredGrant {
        bundle: grant,
        source: GrantSource::ManagedFile,
        settings,
        daemon_reachable: true,
        now: ctx.now,
    })
}

fn persist_interactive(
    ctx: &AcquireCtx,
    grant: GrantBundle,
    bearer: &str,
) -> Result<AcquiredGrant, GrantError> {
    let path = interactive_cache_path(&ctx.home, &grant.deployment.token, &ctx.project_id);
    if let Ok(existing) = load_grant_file(&path)
        && !newer_generation(Some(&existing), &grant)
    {
        return Ok(AcquiredGrant {
            bundle: existing,
            source: GrantSource::Cache,
            settings: load_settings_file(&settings_cache_path(&path)).ok(),
            daemon_reachable: true,
            now: ctx.now,
        });
    }
    let previous = load_grant_file(&path).ok();
    let (grant, settings) = match fetch_settings_coherent(ctx, grant, Some(bearer), previous, false)
    {
        Ok(pair) => pair,
        Err(GrantError::ConfigRevisionMismatch) => return Err(GrantError::ConfigRevisionMismatch),
        Err(error) => return Err(error),
    };
    write_grant_file(&path, &grant)?;
    if let Some(settings) = &settings {
        write_settings_file(&settings_cache_path(&path), settings)?;
    }
    write_binding(
        &ctx.home,
        &TrustedBinding {
            endpoint: cache::normalize_endpoint(&ctx.daemon_url),
            deployment_token: grant.deployment.token.clone(),
        },
    )?;
    Ok(AcquiredGrant {
        bundle: grant,
        source: GrantSource::Handshake,
        settings,
        daemon_reachable: true,
        now: ctx.now,
    })
}

fn fetch_settings_coherent(
    ctx: &AcquireCtx,
    grant: GrantBundle,
    bearer: Option<&str>,
    previous_grant: Option<GrantBundle>,
    managed: bool,
) -> Result<(GrantBundle, Option<CachedSettings>), GrantError> {
    let timeout = ctx.remaining()?;
    let settings = match fetch_runtime_config(&ctx.daemon_url, &grant, bearer, timeout) {
        Ok(settings) => settings,
        Err(GrantError::DaemonRequired | GrantError::Timeout) => return Ok((grant, None)),
        Err(error) => return Err(error),
    };
    if settings.config_revision == grant.config_revision {
        return Ok((grant, Some(settings)));
    }
    let retried = if managed {
        let envelope = std::env::var(crate::local_token::AGENT_API_TOKEN_ENV)
            .map_err(|_| GrantError::ConfigRevisionMismatch)?;
        let claims = parse_envelope(envelope.trim())?;
        challenge_and_handshake(
            &ctx.daemon_url,
            envelope.trim(),
            &ctx.machine_id,
            &ctx.project_id,
            Some(&claims.session_id),
            Some(&claims),
            ctx.deadline,
        )?
    } else {
        let token = interactive_bearer(&ctx.home)?;
        challenge_and_handshake(
            &ctx.daemon_url,
            &token,
            &ctx.machine_id,
            &ctx.project_id,
            ctx.session_id.as_deref().or(Some("cli")),
            None,
            ctx.deadline,
        )?
    };
    let timeout = ctx.remaining()?;
    let retried_bearer = if managed {
        std::env::var(crate::local_token::AGENT_API_TOKEN_ENV).ok()
    } else {
        interactive_bearer(&ctx.home).ok()
    };
    let retried_settings = fetch_runtime_config(
        &ctx.daemon_url,
        &retried,
        retried_bearer.as_deref(),
        timeout,
    )?;
    if retried_settings.config_revision == retried.config_revision {
        return Ok((retried, Some(retried_settings)));
    }
    let _ = previous_grant;
    Err(GrantError::ConfigRevisionMismatch)
}

fn interactive_bearer(home: &Path) -> Result<String, GrantError> {
    crate::local_token::read_local_cli_token_for(home)
        .map_err(|error| GrantError::Malformed(error.to_string()))
}

fn managed_bootstrap_path() -> Option<PathBuf> {
    std::env::var_os(MANAGED_BOOTSTRAP_ENV).map(PathBuf::from)
}

fn validate_managed_identity(grant: &GrantBundle) -> Result<(), GrantError> {
    let env_exec = std::env::var("GOBBY_AGENT_RUN_ID")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .or_else(|| {
            std::env::var("GOBBY_MANAGED_EXECUTION_ID")
                .ok()
                .filter(|value| !value.trim().is_empty())
        });
    if let Some(expected) = env_exec
        && grant.principal.execution_id.as_deref() != Some(expected.trim())
    {
        return Err(GrantError::Malformed(
            "managed grant execution identity mismatch".to_string(),
        ));
    }
    Ok(())
}

fn resolve_home(home: Option<&Path>) -> Result<PathBuf, GrantError> {
    match home {
        Some(home) => Ok(home.to_path_buf()),
        None => crate::gobby_home().map_err(|error| GrantError::Io(error.to_string())),
    }
}

fn unix_now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or(0)
}

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 0x0f) as usize] as char);
    }
    out
}

fn sha256(bytes: &[u8]) -> [u8; 32] {
    openssl::sha::sha256(bytes)
}

fn hmac_sha256(key: &[u8], data: &[u8]) -> Vec<u8> {
    let pkey = openssl::pkey::PKey::hmac(key)
        .unwrap_or_else(|_| panic!("openssl hmac key construction cannot fail for raw bytes"));
    let mut signer = openssl::sign::Signer::new(openssl::hash::MessageDigest::sha256(), &pkey)
        .unwrap_or_else(|_| panic!("openssl hmac-sha256 signer cannot fail"));
    signer
        .update(data)
        .unwrap_or_else(|_| panic!("openssl hmac update cannot fail"));
    signer
        .sign_to_vec()
        .unwrap_or_else(|_| panic!("openssl hmac sign cannot fail"))
}

#[cfg(test)]
#[path = "tests.rs"]
mod tests;
