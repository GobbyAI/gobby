//! gcore grant client: handshake, cache, renewal, and typed errors.

mod bundle;
mod cache;
pub mod fixture;
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
pub use fixture::{DirectConnections, managed_direct_grant, write_managed_bootstrap};
pub use handshake::{
    AGENT_RUN_HEADER, CALLER_PROJECT_HEADER, CapabilityClaims, GRANT_HEADER, MACHINE_HEADER,
    MANAGED_BOOTSTRAP_ENV, MANAGED_EXECUTION_HEADER, SESSION_HEADER, TARGET_PROJECT_HEADER,
    daemon_reachable, encode_grant_header, parse_capability_token, reject_remote_endpoint,
};

use bundle::validate_for_construction as validate_grant;
use cache::{
    CachePair, inspect_cache_pair, lock_with_deadline, matching_settings, newer_generation,
    persist_cache,
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

    pub fn is_stale_epoch(&self) -> bool {
        matches!(self, Self::Malformed(message) if message == "stale_epoch")
    }

    pub fn is_retryable_presentation(&self) -> bool {
        self.is_stale_epoch()
            || matches!(self, Self::Malformed(message) if message == "invalid_signature")
    }

    pub fn from_presentation_http(status: u16, body: &str) -> Option<Self> {
        if body.contains("revoked") {
            return Some(Self::Revoked);
        }
        if status == 409 || body.contains("stale_epoch") {
            return Some(Self::Malformed("stale_epoch".to_string()));
        }
        if body.contains("invalid_signature") {
            return Some(Self::Malformed("invalid_signature".to_string()));
        }
        None
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
    pub managed_bootstrap: Option<PathBuf>,
    pub managed_envelope: Option<String>,
    pub expected_execution_id: Option<String>,
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
            managed_bootstrap: None,
            managed_envelope: None,
            expected_execution_id: None,
        }
    }

    /// Snapshot process environment into an explicit request.
    ///
    /// `acquire_with` never reads the environment; production `acquire` and
    /// daemon presentation retries go through here so tests can isolate.
    pub fn from_process(project_root: &'a Path) -> Self {
        let mut request = Self::new(project_root);
        request.daemon_url = Some(crate::daemon_url::daemon_url());
        request.managed_bootstrap = std::env::var_os(MANAGED_BOOTSTRAP_ENV).map(PathBuf::from);
        request.managed_envelope = std::env::var(crate::local_token::AGENT_API_TOKEN_ENV).ok();
        request.expected_execution_id = std::env::var("GOBBY_AGENT_RUN_ID")
            .ok()
            .filter(|value| !value.trim().is_empty())
            .or_else(|| {
                std::env::var("GOBBY_MANAGED_EXECUTION_ID")
                    .ok()
                    .filter(|value| !value.trim().is_empty())
            });
        request
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
    managed_bootstrap: Option<PathBuf>,
    managed_envelope: Option<String>,
    expected_execution_id: Option<String>,
}

pub fn acquire(project_root: impl AsRef<Path>) -> Result<AcquiredGrant, GrantError> {
    acquire_with(&AcquireRequest::from_process(project_root.as_ref()))
}

pub fn acquire_with(request: &AcquireRequest<'_>) -> Result<AcquiredGrant, GrantError> {
    let ctx = AcquireCtx::from_request(request)?;
    handshake::reject_remote_endpoint(&ctx.daemon_url)?;
    if let Some(path) = managed_bootstrap_path(&ctx) {
        return acquire_managed(&ctx, &path);
    }
    acquire_interactive(&ctx)
}

/// Present ``present`` with the current grant. A stale-epoch or rotated-signature
/// rejection triggers exactly one locked re-handshake, cache replace, and retry.
pub fn present_with_single_retry<T, F>(
    request: &AcquireRequest<'_>,
    present: F,
) -> Result<T, GrantError>
where
    F: FnMut(&GrantBundle) -> Result<T, GrantError>,
{
    let acquired = acquire_with(request)?;
    present_bundle_with_single_retry(request, &acquired.bundle, present)
}

/// Present an already-acquired grant. Restart/takeover stale-epoch or rotated
/// signature errors force exactly one re-handshake and retry.
pub fn present_bundle_with_single_retry<T, F>(
    request: &AcquireRequest<'_>,
    grant: &GrantBundle,
    mut present: F,
) -> Result<T, GrantError>
where
    F: FnMut(&GrantBundle) -> Result<T, GrantError>,
{
    match present(grant) {
        Ok(value) => Ok(value),
        Err(error) if error.is_retryable_presentation() => {
            let refreshed = force_rehandshake(request)?;
            present(&refreshed.bundle)
        }
        Err(error) => Err(error),
    }
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
        if let Some(error) = GrantError::from_presentation_http(response.status, &response.body) {
            return Err(error);
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
            daemon_url: match request.daemon_url.clone() {
                Some(url) => url,
                None => match request.home {
                    Some(home) => crate::daemon_url::daemon_url_at(&home.join("bootstrap.yaml")),
                    None => crate::daemon_url::daemon_url(),
                },
            },
            now: request.now.unwrap_or_else(unix_now),
            session_id: request.session_id.clone(),
            deadline: Instant::now() + request.deadline.unwrap_or(DEFAULT_DEADLINE),
            stale_lock_after: request.stale_lock_after.unwrap_or(DEFAULT_STALE_LOCK),
            managed_bootstrap: request.managed_bootstrap.clone(),
            managed_envelope: request.managed_envelope.clone(),
            expected_execution_id: request.expected_execution_id.clone(),
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
    validate_managed_identity(&grant, ctx.expected_execution_id.as_deref())?;
    let destination = path.to_path_buf();
    match inspect_cache_pair(path)? {
        Some(CachePair::Coherent(grant, settings)) => finish_loaded_with_settings(
            ctx,
            grant,
            settings,
            GrantSource::ManagedFile,
            destination,
            true,
        ),
        Some(CachePair::Incoherent(grant)) if ctx.reachable() => {
            handshake_managed(ctx, Some(&grant), destination)
        }
        _ => finish_loaded(ctx, grant, GrantSource::ManagedFile, destination, true),
    }
}

fn acquire_interactive(ctx: &AcquireCtx) -> Result<AcquiredGrant, GrantError> {
    let binding = load_binding(&ctx.home, &ctx.daemon_url);
    if let Some(binding) = binding {
        let path = interactive_cache_path(&ctx.home, &binding.deployment_token, &ctx.project_id);
        match inspect_cache_pair(&path)? {
            Some(CachePair::Coherent(grant, settings)) => {
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
            Some(CachePair::Incoherent(grant)) => {
                validate_grant(
                    &grant,
                    &ctx.project_id,
                    &ctx.machine_id,
                    Some(&binding.deployment_token),
                    false,
                )?;
                if ctx.reachable() {
                    return handshake_interactive(ctx, Some(&binding.deployment_token), false);
                }
                return finish_loaded(ctx, grant, GrantSource::Cache, path, false);
            }
            Some(CachePair::GrantOnly(grant)) => {
                validate_grant(
                    &grant,
                    &ctx.project_id,
                    &ctx.machine_id,
                    Some(&binding.deployment_token),
                    false,
                )?;
                return finish_loaded(ctx, grant, GrantSource::Cache, path, false);
            }
            None => {
                if ctx.reachable() {
                    return handshake_interactive(ctx, Some(&binding.deployment_token), false);
                }
                return Err(GrantError::DaemonRequired);
            }
        }
    }

    if is_default_local_endpoint(&ctx.daemon_url).unwrap_or(false) && ctx.reachable() {
        return handshake_interactive(ctx, Some(&derived_deployment_token(&ctx.home)), true);
    }
    if ctx.reachable() {
        return handshake_interactive(ctx, None, false);
    }
    Err(GrantError::DaemonRequired)
}

fn force_rehandshake(request: &AcquireRequest<'_>) -> Result<AcquiredGrant, GrantError> {
    let ctx = AcquireCtx::from_request(request)?;
    handshake::reject_remote_endpoint(&ctx.daemon_url)?;
    if !ctx.reachable() {
        return Err(GrantError::DaemonRequired);
    }
    if let Some(path) = managed_bootstrap_path(&ctx) {
        let destination = path.to_path_buf();
        let lock_path = grant_lock_path(&destination);
        let _lock = lock_with_deadline(&lock_path, ctx.stale_lock_after, ctx.deadline)?;
        let existing = load_grant_file(&destination).ok();
        return handshake_managed(&ctx, existing.as_ref(), destination);
    }
    let token = load_binding(&ctx.home, &ctx.daemon_url)
        .map(|binding| binding.deployment_token)
        .unwrap_or_else(|| derived_deployment_token(&ctx.home));
    let destination = interactive_cache_path(&ctx.home, &token, &ctx.project_id);
    let lock_path = grant_lock_path(&destination);
    let _lock = lock_with_deadline(&lock_path, ctx.stale_lock_after, ctx.deadline)?;
    handshake_interactive(&ctx, Some(&token), false)
}

fn finish_loaded(
    ctx: &AcquireCtx,
    grant: GrantBundle,
    source: GrantSource,
    destination: PathBuf,
    managed: bool,
) -> Result<AcquiredGrant, GrantError> {
    if grant.is_expired(ctx.now) {
        return refresh_or_fail(ctx, Some(&grant), source, destination, managed, true, None);
    }
    if let Some(acquired) = maybe_proactive_refresh(ctx, &grant, source, &destination, managed) {
        return Ok(acquired);
    }
    let reachable = ctx.reachable();
    let settings = matching_settings(&destination, &grant);
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
        return refresh_or_fail(ctx, Some(&grant), source, destination, managed, true, None);
    }
    if let Some(acquired) = maybe_proactive_refresh(ctx, &grant, source, &destination, managed) {
        return Ok(acquired);
    }
    Ok(AcquiredGrant {
        bundle: grant,
        source,
        settings: Some(settings),
        daemon_reachable: ctx.reachable(),
        now: ctx.now,
    })
}

fn maybe_proactive_refresh(
    ctx: &AcquireCtx,
    grant: &GrantBundle,
    source: GrantSource,
    destination: &Path,
    managed: bool,
) -> Option<AcquiredGrant> {
    if !(grant.past_half_ttl(ctx.now) && ctx.reachable()) {
        return None;
    }
    let lock = try_lock(&grant_lock_path(destination)).ok().flatten()?;
    refresh_or_fail(
        ctx,
        Some(grant),
        source,
        destination.to_path_buf(),
        managed,
        false,
        Some(lock),
    )
    .ok()
}

fn refresh_or_fail(
    ctx: &AcquireCtx,
    existing: Option<&GrantBundle>,
    source: GrantSource,
    destination: PathBuf,
    managed: bool,
    mandatory: bool,
    held_lock: Option<GrantFileLock>,
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
            let settings = matching_settings(&destination, &grant);
            Ok(AcquiredGrant {
                bundle: grant,
                source,
                settings,
                daemon_reachable: false,
                now: ctx.now,
            })
        };
    }
    let lock_path = grant_lock_path(&destination);
    let _lock = if held_lock.is_some() {
        held_lock
    } else if mandatory {
        Some(lock_with_deadline(
            &lock_path,
            ctx.stale_lock_after,
            ctx.deadline,
        )?)
    } else {
        match try_lock(&lock_path)? {
            Some(lock) => Some(lock),
            None => {
                let grant = existing.cloned().ok_or(GrantError::DaemonRequired)?;
                let settings = matching_settings(&destination, &grant);
                return Ok(AcquiredGrant {
                    bundle: grant,
                    source,
                    settings,
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
        let settings = matching_settings(&destination, &current);
        return Ok(AcquiredGrant {
            bundle: current,
            source,
            settings,
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
    }
}

fn with_presentation_retry<T, F>(mut op: F) -> Result<T, GrantError>
where
    F: FnMut() -> Result<T, GrantError>,
{
    match op() {
        Ok(value) => Ok(value),
        Err(error) if error.is_retryable_presentation() => op(),
        Err(error) => Err(error),
    }
}

fn handshake_interactive(
    ctx: &AcquireCtx,
    expected_deployment: Option<&str>,
    verify_derived: bool,
) -> Result<AcquiredGrant, GrantError> {
    let expected = expected_deployment.map(ToOwned::to_owned);
    with_presentation_retry(|| handshake_interactive_once(ctx, expected.as_deref(), verify_derived))
}

fn handshake_interactive_once(
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
    validate_grant(
        &grant,
        &ctx.project_id,
        &ctx.machine_id,
        expected.as_deref(),
        false,
    )?;
    persist_interactive(ctx, grant, &token)
}

fn managed_envelope(
    ctx: &AcquireCtx,
    existing: Option<&GrantBundle>,
) -> Result<(String, CapabilityClaims), GrantError> {
    let owned = ctx.managed_envelope.clone().ok_or(GrantError::Expired)?;
    let envelope = owned.trim();
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
    with_presentation_retry(|| handshake_managed_once(ctx, existing, destination.clone()))
}

fn handshake_managed_once(
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
    persist_cache(&destination, &grant, settings.as_ref())?;
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
        let settings = matching_settings(&path, &existing);
        return Ok(AcquiredGrant {
            bundle: existing,
            source: GrantSource::Cache,
            settings,
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
    persist_cache(&path, &grant, settings.as_ref())?;
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
        let (envelope, claims) = managed_envelope(ctx, previous_grant.as_ref())?;
        challenge_and_handshake(
            &ctx.daemon_url,
            &envelope,
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
        ctx.managed_envelope.clone()
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
    crate::local_token::read_local_cli_token_at(home)
        .map_err(|error| GrantError::Malformed(error.to_string()))
}

fn managed_bootstrap_path(ctx: &AcquireCtx) -> Option<PathBuf> {
    ctx.managed_bootstrap.clone()
}

fn validate_managed_identity(
    grant: &GrantBundle,
    expected: Option<&str>,
) -> Result<(), GrantError> {
    if let Some(expected) = expected.filter(|value| !value.trim().is_empty())
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
