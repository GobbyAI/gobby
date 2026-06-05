use serde_json::json;

use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{CommandOutcome, ScopeIdentity, ScopeSelection, WikiError};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    render(resolved_scope_identity(&scope))
}

fn render(scope: ScopeIdentity) -> Result<CommandOutcome, WikiError> {
    let daemon_url = gobby_core::daemon_url::daemon_url();
    let runtime = runtime_status()?;
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

struct RuntimeStatus {
    status: &'static str,
    mode: &'static str,
    services: serde_json::Value,
}

fn runtime_status() -> Result<RuntimeStatus, WikiError> {
    let Some(database_url) = crate::support::env::database_url_from_env() else {
        return Ok(RuntimeStatus {
            status: "shell-ready",
            mode: "memory",
            services: json!({}),
        });
    };
    let mut conn = gobby_core::postgres::connect_readonly(&database_url).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to PostgreSQL for gwiki status: {error}"),
        }
    })?;
    let home = gobby_home()?;
    let primary = crate::support::search::PostgresConfigSource { conn: &mut conn };
    let mut source =
        gobby_core::ai_context::AiConfigSource::with_primary_from_gobby_home(primary, &home)
            .map_err(|error| WikiError::Config {
                detail: format!("failed to resolve runtime config for gwiki status: {error}"),
            })?;
    let falkor = gobby_core::config::resolve_falkordb_config(&mut source)
        .ok_or_else(|| required_status_config("FalkorDB"))?;
    let qdrant = gobby_core::config::resolve_qdrant_config(&mut source)
        .filter(|config| {
            config
                .url
                .as_deref()
                .is_some_and(|url| !url.trim().is_empty())
        })
        .ok_or_else(|| required_status_config("Qdrant"))?;
    let embedding = gobby_core::config::resolve_embedding_config(&mut source)
        .ok_or_else(|| required_status_config("embedding endpoint"))?;
    Ok(RuntimeStatus {
        status: "datastore-ready",
        mode: "postgres",
        services: json!({
            "postgres": {"configured": true},
            "falkordb": {"configured": true, "host": falkor.host, "port": falkor.port},
            "qdrant": {"configured": true, "url": qdrant.url},
            "embeddings": {
                "configured": true,
                "api_base": embedding.api_base,
                "model": embedding.model
            },
        }),
    })
}

fn required_status_config(service: &'static str) -> WikiError {
    WikiError::Config {
        detail: format!(
            "gwiki status requires {service} when PostgreSQL is configured; run `gwiki setup --standalone` or attach to Gobby's full datastore stack"
        ),
    }
}

fn gobby_home() -> Result<std::path::PathBuf, WikiError> {
    gobby_core::gobby_home().map_err(|error| WikiError::Config {
        detail: format!("failed to resolve Gobby home for gwiki status: {error}"),
    })
}
