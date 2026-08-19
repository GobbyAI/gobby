use std::time::Duration;

use gobby_core::grant::{
    CachedGrantInspection, daemon_reachable, deployment_token, inspect_cached_grant,
    interactive_cache_path, load_binding, load_grant_file,
};
use serde_json::json;

use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{CommandOutcome, ScopeIdentity, ScopeSelection, WikiError};

const REACHABILITY_PROBE: Duration = Duration::from_millis(150);

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    render(resolved_scope_identity(&scope))
}

fn render(scope: ScopeIdentity) -> Result<CommandOutcome, WikiError> {
    let snapshot = grant_status_snapshot();
    let payload = json!({
        "command": "status",
        "scope": scope,
        "status": snapshot.state,
        "daemon_url": snapshot.daemon_url,
        "grant": {
            "state": snapshot.state,
            "reason": snapshot.reason,
            "deployment_token": snapshot.deployment_token,
            "epoch": snapshot.epoch,
            "expires_at": snapshot.expires_at,
            "remaining_ttl": snapshot.remaining_ttl,
        },
        "daemon": {
            "reachable": snapshot.reachable,
        },
    });
    let text = format!(
        "gwiki {state}
Scope: {scope}
Daemon: {daemon_url}
{grant_line}
Reachable: {reachable}",
        state = snapshot.state,
        daemon_url = snapshot.daemon_url,
        grant_line = grant_text_line(snapshot.state, snapshot.reason.as_deref()),
        reachable = snapshot.reachable,
    );
    Ok(super::scoped_outcome("status", &scope, payload, text))
}

pub(crate) struct GrantStatusSnapshot {
    pub(crate) state: &'static str,
    pub(crate) daemon_url: String,
    pub(crate) deployment_token: Option<String>,
    pub(crate) epoch: Option<i64>,
    pub(crate) expires_at: Option<i64>,
    pub(crate) remaining_ttl: Option<i64>,
    pub(crate) reachable: bool,
    pub(crate) reason: Option<String>,
}

fn grant_text_line(state: &str, reason: Option<&str>) -> String {
    match reason {
        Some(reason) => format!("Grant: {state}: {reason}"),
        None => format!("Grant: {state}"),
    }
}

pub(crate) fn grant_status_snapshot() -> GrantStatusSnapshot {
    let daemon_url = gobby_core::daemon_url::daemon_url();
    let reachable = daemon_reachable(&daemon_url, REACHABILITY_PROBE);
    let Some(root) = crate::support::env::project_root_for_grant() else {
        return GrantStatusSnapshot {
            state: "absent",
            daemon_url,
            deployment_token: None,
            epoch: None,
            expires_at: None,
            remaining_ttl: None,
            reachable,
            reason: None,
        };
    };
    let inspection = inspect_cached_grant(&root);
    let (state, reason) = match &inspection {
        CachedGrantInspection::Absent => ("absent", None),
        CachedGrantInspection::Malformed { reason } => ("malformed", Some(reason.clone())),
        CachedGrantInspection::Valid { .. } => ("valid", None),
        CachedGrantInspection::Expiring { .. } => ("expiring", None),
        CachedGrantInspection::Expired { .. } => ("expired", None),
    };
    let (expires_at, remaining_ttl) = match &inspection {
        CachedGrantInspection::Valid {
            expires_at,
            remaining_ttl,
        }
        | CachedGrantInspection::Expiring {
            expires_at,
            remaining_ttl,
        } => (Some(*expires_at), Some(*remaining_ttl)),
        CachedGrantInspection::Expired { expires_at } => (Some(*expires_at), None),
        CachedGrantInspection::Absent | CachedGrantInspection::Malformed { .. } => (None, None),
    };
    let bundle = load_cached_grant_bundle(&root, &daemon_url);
    GrantStatusSnapshot {
        state,
        daemon_url,
        deployment_token: bundle.as_ref().map(|grant| grant.deployment.token.clone()),
        epoch: bundle.as_ref().map(|grant| grant.deployment.fencing_epoch),
        expires_at,
        remaining_ttl,
        reachable,
        reason,
    }
}

fn load_cached_grant_bundle(
    project_root: &std::path::Path,
    daemon_url: &str,
) -> Option<gobby_core::grant::GrantBundle> {
    let home = gobby_core::gobby_home().ok()?;
    let project_id = gobby_core::project::read_project_id(project_root).ok()?;
    let token = load_binding(&home, daemon_url)
        .map(|binding| binding.deployment_token)
        .unwrap_or_else(|| deployment_token(&home));
    let path = interactive_cache_path(&home, &token, &project_id);
    load_grant_file(&path).ok()
}

#[cfg(test)]
mod tests {
    use super::grant_text_line;

    #[test]
    fn malformed_reason_is_rendered_in_status_text() {
        let reason = "grant payload skew: unknown field `credential_generation`";
        let text = grant_text_line("malformed", Some(reason));
        assert_eq!(text, format!("Grant: malformed: {reason}"));
    }
}
