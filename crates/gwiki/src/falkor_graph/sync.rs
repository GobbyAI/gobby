use gobby_core::config::FalkorConfig;
use gobby_core::falkor::{
    GraphClient, escape_label, escape_property, escape_rel_type, escape_string,
};
use postgres::Client;

use crate::WikiError;
use crate::graph::{
    GraphStatement, MENTIONS_TARGET_REL, SUPPORTS_REL, WIKI_DOC_LABEL, WIKI_LINKS_TO_REL,
    WIKI_SOURCE_LABEL, WIKI_TARGET_LABEL, graph_write_statements,
};
use crate::search::SearchScope;

use super::FALKORDB_GRAPH_NAME;
use super::query::scope_params;
use super::wiki_facts::load_wiki_graph_facts;

/// Incrementally reconciles the scope's wiki projection in FalkorDB.
///
/// Replaces the previous whole-scope `DETACH DELETE` + rebuild. In order:
/// 1. delete only the scope's wiki-owned edges (so stale links/mentions/supports
///    vanish while nodes survive — no empty-scope node window),
/// 2. delete `WikiDoc` nodes for paths whose latest ingestion is `deleted`,
/// 3. replay `graph_write_statements` (idempotent `MERGE`s recreate the current
///    node/edge set), then
/// 4. remove orphaned `WikiSource`/`WikiTarget` nodes left without a current
///    `SUPPORTS`/`MENTIONS_TARGET` edge.
///
/// No transaction is used (the `GraphClient` wrapper exposes none); the only
/// residual window is edges briefly absent between steps 1 and 3 of a single
/// sync. The global scope cannot be synced as a scoped projection.
pub(crate) fn sync_scope_from_postgres(
    conn: &mut Client,
    scope: &SearchScope,
    config: &FalkorConfig,
) -> Result<(), WikiError> {
    require_scoped(scope).map_err(graph_sync_error)?;
    let facts = load_wiki_graph_facts(conn, scope)?;
    let deleted_paths = load_deleted_paths(conn, scope)?;
    let mut client = GraphClient::from_config(config, FALKORDB_GRAPH_NAME).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to FalkorDB for gwiki graph sync: {error}"),
        }
    })?;

    for statement in scope_edge_cleanup_statements(scope) {
        execute_statement(&mut client, statement).map_err(graph_sync_error)?;
    }
    for path in &deleted_paths {
        execute_statement(&mut client, stale_doc_delete_statement(scope, path))
            .map_err(graph_sync_error)?;
    }
    for statement in graph_write_statements(&facts) {
        execute_statement(&mut client, statement).map_err(graph_sync_error)?;
    }
    for statement in orphan_cleanup_statements(scope) {
        execute_statement(&mut client, statement).map_err(graph_sync_error)?;
    }
    Ok(())
}

pub(crate) fn purge_scope(scope: &SearchScope, config: &FalkorConfig) -> Result<(), WikiError> {
    require_scoped(scope).map_err(graph_sync_error)?;
    let mut client = GraphClient::from_config(config, FALKORDB_GRAPH_NAME).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to FalkorDB for gwiki purge: {error}"),
        }
    })?;

    for statement in scope_purge_statements(scope) {
        execute_statement(&mut client, statement).map_err(graph_sync_error)?;
    }
    Ok(())
}

/// Latest-deleted document paths in the scope, mirroring the Qdrant stale-path
/// query shape: the most recent ingestion per path whose status is `deleted`.
fn load_deleted_paths(conn: &mut Client, scope: &SearchScope) -> Result<Vec<String>, WikiError> {
    let rows = conn
        .query(
            "SELECT path
             FROM (
                SELECT DISTINCT ON (path) path, status, ingested_at
                FROM gwiki_ingestions
                WHERE scope_kind = $1 AND scope_id = $2
                ORDER BY path, ingested_at DESC
             ) latest
             WHERE status = 'deleted'
             ORDER BY path",
            &[&scope.scope_kind(), &scope.scope_value()],
        )
        .map_err(|error| WikiError::Config {
            detail: format!("failed to load deleted gwiki paths for FalkorDB sync: {error}"),
        })?;
    Ok(rows.into_iter().map(|row| row.get("path")).collect())
}

pub(super) fn require_scoped(scope: &SearchScope) -> anyhow::Result<()> {
    if scope_params(scope).is_none() {
        anyhow::bail!("global wiki graph scope cannot be synced as a scoped projection");
    }
    Ok(())
}

/// Deletes the scope's wiki-owned edges (nodes survive). Replay re-`MERGE`s the
/// current edge set, so any edge not in the current facts stays deleted.
pub(super) fn scope_edge_cleanup_statements(scope: &SearchScope) -> Vec<GraphStatement> {
    let scope_props = scope_property_map(scope);
    [WIKI_LINKS_TO_REL, MENTIONS_TARGET_REL, SUPPORTS_REL]
        .into_iter()
        .map(|rel| GraphStatement {
            cypher: format!(
                "MATCH (node {{{scope_props}}})-[edge:{rel}]->() DELETE edge",
                rel = escape_rel_type(rel),
            ),
        })
        .collect()
}

pub(super) fn scope_purge_statements(scope: &SearchScope) -> Vec<GraphStatement> {
    let scope_props = scope_property_map(scope);
    let mut statements = scope_edge_cleanup_statements(scope);
    statements.push(GraphStatement {
        cypher: format!("MATCH (node {{{scope_props}}}) DETACH DELETE node"),
    });
    statements
}

/// Detaches and deletes the scope's `WikiDoc` node for a deleted path. One
/// statement per path because `GraphClient::query` interpolates only string
/// params; the escaped path is inlined like the replay statements.
pub(super) fn stale_doc_delete_statement(scope: &SearchScope, path: &str) -> GraphStatement {
    GraphStatement {
        cypher: format!(
            "MATCH (doc:{label} {{{scope_props}, {path_key}: {path_value}}}) DETACH DELETE doc",
            label = escape_label(WIKI_DOC_LABEL),
            scope_props = scope_property_map(scope),
            path_key = escape_property("path"),
            path_value = escape_string(path),
        ),
    }
}

/// After replay, removes orphaned scoped nodes: `WikiSource` with no outgoing
/// `SUPPORTS` edge, and `WikiTarget` with no incoming `MENTIONS_TARGET` edge.
pub(super) fn orphan_cleanup_statements(scope: &SearchScope) -> Vec<GraphStatement> {
    let scope_props = scope_property_map(scope);
    vec![
        GraphStatement {
            cypher: format!(
                "MATCH (source:{label} {{{scope_props}}}) WHERE NOT (source)-[:{rel}]->() DELETE source",
                label = escape_label(WIKI_SOURCE_LABEL),
                rel = escape_rel_type(SUPPORTS_REL),
            ),
        },
        GraphStatement {
            cypher: format!(
                "MATCH (target:{label} {{{scope_props}}}) WHERE NOT ()-[:{rel}]->(target) DELETE target",
                label = escape_label(WIKI_TARGET_LABEL),
                rel = escape_rel_type(MENTIONS_TARGET_REL),
            ),
        },
    ]
}

/// `scope_kind`/`scope_id` property-map fragment with Cypher-escaped values,
/// matching how `graph_write_statements` scopes its nodes.
fn scope_property_map(scope: &SearchScope) -> String {
    format!(
        "{}: {}, {}: {}",
        escape_property("scope_kind"),
        escape_string(scope.scope_kind()),
        escape_property("scope_id"),
        escape_string(scope.scope_value()),
    )
}

fn execute_statement(client: &mut GraphClient, statement: GraphStatement) -> anyhow::Result<()> {
    client.query(&statement.cypher, None)?;
    Ok(())
}

fn graph_sync_error(error: anyhow::Error) -> WikiError {
    WikiError::Config {
        detail: format!("failed to sync gwiki graph to FalkorDB: {error}"),
    }
}
