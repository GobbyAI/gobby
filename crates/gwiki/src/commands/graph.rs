use gobby_core::ai_context::{AiConfigSource, AiContext};
use gobby_core::config::{AiRouting, QdrantConfig, resolve_embedding_config};
use gobby_core::gobby_home;
use serde_json::json;

use crate::graph::GraphExportOptions;
use crate::support::env::database_url_for;
use crate::support::scope::resolve_selection_context;
use crate::support::search::PostgresConfigSource;
use crate::{CommandOutcome, ScopeSelection, WikiError, exports};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let resolved = resolve_selection_context(&selection)?;
    let database_url = database_url_for("gwiki graph")?.ok_or_else(|| WikiError::Config {
        detail: "gwiki graph requires PostgreSQL index configuration".to_string(),
    })?;
    let mut conn = gobby_core::postgres::connect_readonly(&database_url).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to PostgreSQL for gwiki graph: {error}"),
        }
    })?;

    let degraded_sources = degraded_optional_sources(&mut conn)?;
    let facts = crate::falkor_graph::load_wiki_graph_facts(&mut conn, &resolved.search_scope)?;
    let options = if degraded_sources.is_empty() {
        GraphExportOptions::available()
    } else {
        GraphExportOptions::degraded(degraded_sources)
    };
    let artifacts = exports::export_graph_artifacts(resolved.scope.root(), &facts, options)?;
    let paths = artifacts
        .iter()
        .map(|artifact| artifact.path.display().to_string())
        .collect::<Vec<_>>();
    let payload = json!({
        "command": "graph",
        "scope": resolved.output_scope,
        "artifacts": artifacts,
    });
    let text = format!(
        "Exported wiki graph artifacts\nScope: {}\nArtifacts: {}",
        resolved.output_scope,
        paths.join(", ")
    );
    Ok(super::scoped_outcome(
        "graph",
        &resolved.output_scope,
        payload,
        text,
    ))
}

fn degraded_optional_sources(conn: &mut postgres::Client) -> Result<Vec<String>, WikiError> {
    let gobby_home = gobby_home().map_err(|error| WikiError::Config {
        detail: format!("failed to resolve Gobby home for gwiki graph: {error}"),
    })?;
    let primary = PostgresConfigSource { conn };
    let mut source =
        AiConfigSource::with_primary_from_gobby_home(primary, &gobby_home).map_err(|error| {
            WikiError::Config {
                detail: format!("failed to resolve optional graph export config: {error}"),
            }
        })?;

    let mut degraded = Vec::new();
    if gobby_core::config::resolve_falkordb_config(&mut source).is_none() {
        degraded.push("falkordb_unavailable".to_string());
    }

    let ai_context = AiContext::resolve(None, &mut source);
    let has_embedding = match ai_context
        .binding(gobby_core::config::AiCapability::Embed)
        .routing
    {
        AiRouting::Off => false,
        AiRouting::Daemon => {
            #[cfg(feature = "ai")]
            {
                true
            }
            #[cfg(not(feature = "ai"))]
            {
                false
            }
        }
        AiRouting::Direct => resolve_embedding_config(&mut source).is_some(),
        AiRouting::Auto => {
            #[cfg(feature = "ai")]
            {
                true
            }
            #[cfg(not(feature = "ai"))]
            {
                resolve_embedding_config(&mut source).is_some()
            }
        }
    };
    let has_qdrant = gobby_core::config::resolve_qdrant_config(&mut source)
        .filter(qdrant_config_has_url)
        .is_some();
    if !has_embedding || !has_qdrant {
        degraded.push("semantic_relations_unavailable".to_string());
    }

    Ok(degraded)
}

fn qdrant_config_has_url(config: &QdrantConfig) -> bool {
    config
        .url
        .as_deref()
        .is_some_and(|url| !url.trim().is_empty())
}
