use gobby_core::ai_context::AiConfigSource;
use gobby_core::config::FalkorConfig;
use gobby_core::gobby_home;

use crate::graph::context::{GraphContextOptions, build_context_pack};
use crate::search::SearchScope;
use crate::support::config::shared_code_graph_limits_from_conn;
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

    let falkor = optional_falkor_config(&mut conn)?;
    let mut degraded_sources = Vec::new();
    let mut truncated_components = Vec::new();
    let limits = shared_code_graph_limits_from_conn(&mut conn)?;
    let mut facts = crate::falkor_graph::load_wiki_graph_facts(&mut conn, &resolved.search_scope)?;
    match (falkor, &resolved.search_scope) {
        (Some(falkor), SearchScope::Project { project_id }) => {
            match crate::falkor_graph::load_code_graph_edges(
                &falkor,
                project_id,
                &facts.documents,
                limits,
            ) {
                Ok(code_graph) => {
                    if code_graph.truncation.is_truncated() {
                        degraded_sources.push(
                            crate::falkor_graph::SHARED_CODE_GRAPH_TRUNCATED_SOURCE.to_string(),
                        );
                        truncated_components.extend(code_graph.truncation.components);
                    }
                    facts.code_edges = code_graph.edges;
                }
                Err(error) => {
                    log::warn!("failed to load shared code graph for gwiki graph-context: {error}");
                    degraded_sources.push("shared_code_graph_unavailable".to_string());
                }
            }
        }
        (Some(_), SearchScope::Global | SearchScope::Topic { .. }) => {
            degraded_sources.push("shared_code_graph_unavailable".to_string());
        }
        (None, _) => {
            degraded_sources.push("falkordb_unavailable".to_string());
            degraded_sources.push("shared_code_graph_unavailable".to_string());
        }
    }
    let options = if degraded_sources.is_empty() {
        GraphContextOptions::available()
    } else {
        GraphContextOptions::degraded(degraded_sources)
    }
    .with_truncated_components(truncated_components);
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

fn optional_falkor_config(conn: &mut postgres::Client) -> Result<Option<FalkorConfig>, WikiError> {
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

    Ok(gobby_core::config::resolve_falkordb_config(&mut source))
}
