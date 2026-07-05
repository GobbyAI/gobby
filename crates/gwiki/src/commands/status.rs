use serde_json::json;

use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{CommandOutcome, ScopeIdentity, ScopeSelection, WikiError};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    render(resolved_scope_identity(&scope))
}

fn render(scope: ScopeIdentity) -> Result<CommandOutcome, WikiError> {
    let daemon_url = gobby_core::daemon_url::daemon_url();
    let runtime = runtime_status_for("gwiki status")?;
    let payload = json!({
        "command": "status",
        "scope": scope,
        "status": runtime.status,
        "daemon_url": daemon_url,
        "runtime": runtime.mode,
        "services": runtime.services,
    });
    let text = format!(
        "gwiki {}
Scope: {scope}
Daemon: {daemon_url}
Runtime: {}",
        runtime.status, runtime.mode,
    );
    Ok(super::scoped_outcome("status", &scope, payload, text))
}

pub(crate) struct RuntimeStatus {
    pub(crate) status: &'static str,
    pub(crate) mode: &'static str,
    pub(crate) services: serde_json::Value,
}

pub(crate) fn runtime_status_for(command: &'static str) -> Result<RuntimeStatus, WikiError> {
    let services = crate::support::services::probe_runtime_services(command)?;
    if !services.postgres_configured {
        return Ok(RuntimeStatus {
            status: "shell-ready",
            mode: "memory",
            services: json!({}),
        });
    }
    Ok(RuntimeStatus {
        status: "datastore-ready",
        mode: "postgres",
        services: json!({
            "postgres": {"configured": true},
            "falkordb": services.falkor
                .map(|config| json!({"configured": true, "host": config.host, "port": config.port}))
                .unwrap_or_else(|| json!({"configured": false})),
            "qdrant": services.qdrant
                .map(|config| json!({"configured": true, "url": config.url}))
                .unwrap_or_else(|| json!({"configured": false})),
            "embeddings": services.embedding
                .map(|config| {
                    json!({
                        "configured": true,
                        "api_base": config.api_base,
                        "model": config.model
                    })
                })
                .unwrap_or_else(|| json!({"configured": false})),
        }),
    })
}
