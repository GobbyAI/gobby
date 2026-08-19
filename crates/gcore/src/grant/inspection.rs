//! Cached grant inspection for status surfaces.

use std::path::Path;

use super::cache::{interactive_cache_path, load_binding, load_grant_file};
use super::handshake::{deployment_token as derived_deployment_token, is_default_local_endpoint};
use super::{resolve_home, unix_now};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CachedGrantInspection {
    Absent,
    Malformed,
    Valid { expires_at: i64, remaining_ttl: i64 },
    Expiring { expires_at: i64, remaining_ttl: i64 },
    Expired { expires_at: i64 },
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
