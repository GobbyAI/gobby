use gobby_core::ai_context::AiConfigSource;
use gobby_core::config::{
    FalkorConfig, QdrantConfig, resolve_falkordb_config, resolve_qdrant_config,
};
use postgres::Client;
use serde::Serialize;

use crate::search::SearchScope;
use crate::setup::GwikiTable;
use crate::support::config::qdrant_config_has_url;
use crate::support::postgres::require_postgres_index_readwrite;
use crate::support::scope::{
    resolve_command_scope, resolved_scope_identity, search_scope_for_resolved,
};
use crate::support::search::PostgresConfigSource;
use crate::{CommandOutcome, ScopeIdentity, ScopeSelection, WikiError};

const COMMAND: &str = "gwiki purge";

#[derive(Debug, Serialize)]
struct PurgeSummary {
    command: &'static str,
    scope: ScopeOutput,
    postgres: PostgresPurgeSummary,
    qdrant: BackendPurgeSummary,
    falkor: BackendPurgeSummary,
}

#[derive(Debug, Serialize)]
struct ScopeOutput {
    kind: String,
    id: String,
}

#[derive(Debug, Serialize)]
pub(crate) struct PostgresPurgeSummary {
    documents: u64,
    chunks: u64,
    links: u64,
    sources: u64,
    ingestions: u64,
}

#[derive(Debug, Serialize)]
pub(crate) struct BackendPurgeSummary {
    status: &'static str,
    target: Option<String>,
}

struct BackendConfigs {
    qdrant: Option<QdrantConfig>,
    falkor: Option<FalkorConfig>,
}

pub(crate) fn execute(selection: ScopeSelection, yes: bool) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let search_scope = search_scope_for_resolved(&scope);

    if matches!(search_scope, SearchScope::Global) {
        return Err(WikiError::InvalidScope {
            detail: "gwiki purge requires an explicit project or topic scope".to_string(),
        });
    }

    if !yes {
        return Err(WikiError::InvalidInput {
            field: "purge",
            message: format!(
                "would purge generated gwiki state for scope {output_scope}; pass --yes to confirm"
            ),
        });
    }

    let mut conn = require_postgres_index_readwrite(COMMAND)?;
    let backend_configs = optional_backend_configs(&mut conn)?;
    let postgres = purge_postgres_scope(&mut conn, &output_scope)?;
    let qdrant = purge_qdrant_scope(backend_configs.qdrant.as_ref(), &search_scope)?;
    let falkor = purge_falkor_scope(backend_configs.falkor.as_ref(), &search_scope)?;
    let summary = PurgeSummary {
        command: COMMAND,
        scope: ScopeOutput {
            kind: output_scope.kind.as_str().to_string(),
            id: output_scope.id.clone(),
        },
        postgres,
        qdrant,
        falkor,
    };
    let payload = serde_json::to_value(&summary).map_err(|error| WikiError::Json {
        action: "serialize gwiki purge summary",
        path: None,
        source: error,
    })?;
    let text = render_text(&output_scope, &summary);
    Ok(super::scoped_outcome(COMMAND, &output_scope, payload, text))
}

pub(crate) fn purge_postgres_scope(
    conn: &mut Client,
    scope: &ScopeIdentity,
) -> Result<PostgresPurgeSummary, WikiError> {
    Ok(PostgresPurgeSummary {
        chunks: delete_scope_rows(conn, GwikiTable::Chunks, scope)?,
        links: delete_scope_rows(conn, GwikiTable::Links, scope)?,
        sources: delete_scope_rows(conn, GwikiTable::Sources, scope)?,
        ingestions: delete_scope_rows(conn, GwikiTable::Ingestions, scope)?,
        documents: delete_scope_rows(conn, GwikiTable::Documents, scope)?,
    })
}

fn delete_scope_rows(
    conn: &mut Client,
    table: GwikiTable,
    scope: &ScopeIdentity,
) -> Result<u64, WikiError> {
    conn.execute(
        &format!(
            "DELETE FROM {} WHERE scope_kind = $1 AND scope_id = $2",
            table.name()
        ),
        &[&scope.kind.as_str(), &scope.id],
    )
    .map_err(|error| WikiError::Config {
        detail: format!("failed to purge scoped rows from {}: {error}", table.name()),
    })
}

fn optional_backend_configs(conn: &mut Client) -> Result<BackendConfigs, WikiError> {
    let gobby_home = gobby_core::gobby_home().map_err(|error| WikiError::Config {
        detail: format!("failed to resolve Gobby home for gwiki purge config: {error}"),
    })?;

    let qdrant = {
        let primary = PostgresConfigSource { conn: &mut *conn };
        let mut source = AiConfigSource::with_primary_from_gobby_home(primary, &gobby_home)
            .map_err(|error| WikiError::Config {
                detail: format!(
                    "failed to resolve optional Qdrant config for gwiki purge: {error}"
                ),
            })?;
        resolve_qdrant_config(&mut source)
    };
    let falkor = {
        let primary = PostgresConfigSource { conn: &mut *conn };
        let mut source = AiConfigSource::with_primary_from_gobby_home(primary, &gobby_home)
            .map_err(|error| WikiError::Config {
                detail: format!(
                    "failed to resolve optional FalkorDB config for gwiki purge: {error}"
                ),
            })?;
        resolve_falkordb_config(&mut source)
    };

    Ok(BackendConfigs { qdrant, falkor })
}

pub(crate) fn purge_qdrant_scope(
    config: Option<&QdrantConfig>,
    scope: &SearchScope,
) -> Result<BackendPurgeSummary, WikiError> {
    let Some(collection) = crate::vector::collection_for_scope(scope) else {
        return Ok(BackendPurgeSummary {
            status: "skipped",
            target: None,
        });
    };
    let Some(config) = config.filter(|config| qdrant_config_has_url(config)) else {
        return Ok(BackendPurgeSummary {
            status: "skipped",
            target: Some(collection),
        });
    };

    gobby_core::qdrant::delete_collection(config, &collection).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to purge Qdrant collection {collection}: {error}"),
        }
    })?;
    Ok(BackendPurgeSummary {
        status: "purged",
        target: Some(collection),
    })
}

pub(crate) fn purge_falkor_scope(
    config: Option<&FalkorConfig>,
    scope: &SearchScope,
) -> Result<BackendPurgeSummary, WikiError> {
    let Some(config) = config else {
        return Ok(BackendPurgeSummary {
            status: "skipped",
            target: Some(crate::falkor_graph::FALKORDB_GRAPH_NAME.to_string()),
        });
    };

    crate::falkor_graph::purge_scope(scope, config)?;
    Ok(BackendPurgeSummary {
        status: "purged",
        target: Some(crate::falkor_graph::FALKORDB_GRAPH_NAME.to_string()),
    })
}

fn render_text(scope: &ScopeIdentity, summary: &PurgeSummary) -> String {
    format!(
        "Purged gwiki generated state\nScope: {scope}\nPostgreSQL: documents={}, chunks={}, links={}, sources={}, ingestions={}\nQdrant: {}\nFalkorDB: {}",
        summary.postgres.documents,
        summary.postgres.chunks,
        summary.postgres.links,
        summary.postgres.sources,
        summary.postgres.ingestions,
        backend_text(&summary.qdrant),
        backend_text(&summary.falkor),
    )
}

fn backend_text(summary: &BackendPurgeSummary) -> String {
    match summary.target.as_deref() {
        Some(target) => format!("{} ({target})", summary.status),
        None => summary.status.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn qdrant_collection_names_match_scope_contract() {
        let project = purge_qdrant_scope(None, &SearchScope::project("project-1")).unwrap();
        assert_eq!(project.status, "skipped");
        assert_eq!(project.target.as_deref(), Some("gwiki_project_project-1"));

        let topic = purge_qdrant_scope(None, &SearchScope::topic("rust-notes")).unwrap();
        assert_eq!(topic.status, "skipped");
        assert_eq!(topic.target.as_deref(), Some("gwiki_topic_rust-notes"));
    }

    #[test]
    fn postgres_purge_deletes_only_matching_scope_rows() -> anyhow::Result<()> {
        let Some(database_url) = std::env::var("GOBBY_TEST_DATABASE_URL").ok() else {
            eprintln!("skipping postgres purge test; GOBBY_TEST_DATABASE_URL is not set");
            return Ok(());
        };
        let mut conn = gobby_core::postgres::connect_readwrite(&database_url)?;
        create_temp_gwiki_tables(&mut conn)?;

        let target = ScopeIdentity::project("purge-test");
        let other = ScopeIdentity::topic("other-topic");
        for table in [
            GwikiTable::Documents,
            GwikiTable::Chunks,
            GwikiTable::Links,
            GwikiTable::Sources,
            GwikiTable::Ingestions,
        ] {
            seed_scope_row(&mut conn, table, &target)?;
            seed_scope_row(&mut conn, table, &other)?;
        }

        let summary = purge_postgres_scope(&mut conn, &target)?;

        assert_eq!(summary.documents, 1);
        assert_eq!(summary.chunks, 1);
        assert_eq!(summary.links, 1);
        assert_eq!(summary.sources, 1);
        assert_eq!(summary.ingestions, 1);
        assert_eq!(count_all_scope_rows(&mut conn, &target)?, 0);
        assert_eq!(count_all_scope_rows(&mut conn, &other)?, 5);
        Ok(())
    }

    fn create_temp_gwiki_tables(conn: &mut Client) -> anyhow::Result<()> {
        for table in [
            GwikiTable::Documents,
            GwikiTable::Chunks,
            GwikiTable::Links,
            GwikiTable::Sources,
            GwikiTable::Ingestions,
        ] {
            conn.batch_execute(&format!(
                "CREATE TEMP TABLE {} (
                    id TEXT PRIMARY KEY,
                    scope_kind TEXT NOT NULL,
                    scope_id TEXT NOT NULL
                ) ON COMMIT DROP",
                table.name()
            ))?;
        }
        Ok(())
    }

    fn seed_scope_row(
        conn: &mut Client,
        table: GwikiTable,
        scope: &ScopeIdentity,
    ) -> anyhow::Result<()> {
        let id = format!("{}:{}:{}", table.name(), scope.kind.as_str(), scope.id);
        conn.execute(
            &format!(
                "INSERT INTO {} (id, scope_kind, scope_id) VALUES ($1, $2, $3)",
                table.name()
            ),
            &[&id, &scope.kind.as_str(), &scope.id],
        )?;
        Ok(())
    }

    fn count_all_scope_rows(conn: &mut Client, scope: &ScopeIdentity) -> anyhow::Result<i64> {
        let mut total = 0;
        for table in [
            GwikiTable::Documents,
            GwikiTable::Chunks,
            GwikiTable::Links,
            GwikiTable::Sources,
            GwikiTable::Ingestions,
        ] {
            let row = conn.query_one(
                &format!(
                    "SELECT COUNT(*)::BIGINT FROM {} WHERE scope_kind = $1 AND scope_id = $2",
                    table.name()
                ),
                &[&scope.kind.as_str(), &scope.id],
            )?;
            total += row.get::<_, i64>(0);
        }
        Ok(total)
    }
}
