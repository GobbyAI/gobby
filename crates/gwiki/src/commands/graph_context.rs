use gobby_core::ai_context::AiConfigSource;
use gobby_core::gobby_home;

use crate::graph::context::{GraphContextOptions, build_context_pack};
use crate::support::env::database_url_for;
use crate::support::scope::resolve_selection_context;
use crate::support::search::PostgresConfigSource;
use crate::{CommandOutcome, ScopeSelection, WikiError};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let resolved = resolve_selection_context(&selection)?;
    let database_url =
        database_url_for("gwiki graph-context")?.ok_or_else(|| WikiError::Config {
            detail: "gwiki graph-context requires PostgreSQL index configuration".to_string(),
        })?;
    let mut conn = gobby_core::postgres::connect_readonly(&database_url).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to PostgreSQL for gwiki graph-context: {error}"),
        }
    })?;

    let degraded_sources = degraded_optional_sources(&mut conn)?;
    let facts = crate::falkor_graph::load_wiki_graph_facts(&mut conn, &resolved.search_scope)?;
    let options = if degraded_sources.is_empty() {
        GraphContextOptions::available()
    } else {
        GraphContextOptions::degraded(degraded_sources)
    };
    let pack = build_context_pack(&facts, options);
    let text = format!(
        "Built graph context pack\nScope: {}\nNeighborhoods: {}\nWarnings: {}",
        resolved.output_scope,
        pack.neighborhoods.len(),
        pack.warnings.len()
    );

    Ok(super::scoped_outcome(
        "graph-context",
        &resolved.output_scope,
        serde_json::to_value(pack).map_err(|error| WikiError::Config {
            detail: format!("failed to serialize graph context pack: {error}"),
        })?,
        text,
    ))
}

fn degraded_optional_sources(conn: &mut postgres::Client) -> Result<Vec<String>, WikiError> {
    let gobby_home = gobby_home().map_err(|error| WikiError::Config {
        detail: format!("failed to resolve Gobby home for gwiki graph-context: {error}"),
    })?;
    let primary = PostgresConfigSource { conn };
    let mut source =
        AiConfigSource::with_primary_from_gobby_home(primary, &gobby_home).map_err(|error| {
            WikiError::Config {
                detail: format!("failed to resolve optional graph-context config: {error}"),
            }
        })?;

    let mut degraded = Vec::new();
    if gobby_core::config::resolve_falkordb_config(&mut source).is_none() {
        degraded.push("falkordb_unavailable".to_string());
    }
    degraded.push("shared_code_graph_unavailable".to_string());
    Ok(degraded)
}
