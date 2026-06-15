use gobby_core::config::FalkorConfig;
use gobby_core::falkor::GraphClient;
use postgres::Client;

use crate::WikiError;
use crate::graph::{GraphStatement, graph_write_statements};
use crate::search::SearchScope;

use super::FALKORDB_GRAPH_NAME;
use super::query::scope_params;
use super::wiki_facts::load_wiki_graph_facts;

pub(crate) fn sync_scope_from_postgres(
    conn: &mut Client,
    scope: &SearchScope,
    config: &FalkorConfig,
) -> Result<(), WikiError> {
    let facts = load_wiki_graph_facts(conn, scope)?;
    let mut client = GraphClient::from_config(config, FALKORDB_GRAPH_NAME).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to FalkorDB for gwiki graph sync: {error}"),
        }
    })?;
    clear_scope(&mut client, scope).map_err(graph_sync_error)?;
    for statement in graph_write_statements(&facts) {
        execute_statement(&mut client, statement).map_err(graph_sync_error)?;
    }
    Ok(())
}

fn clear_scope(client: &mut GraphClient, scope: &SearchScope) -> anyhow::Result<()> {
    let Some(params) = scope_params(scope) else {
        anyhow::bail!("global wiki graph scope cannot be cleared as a scoped projection");
    };
    client.query(
        "MATCH (node)
         WHERE (node:WikiDoc OR node:WikiSource OR node:WikiTarget)
           AND node.scope_kind = $scope_kind
           AND node.scope_id = $scope_id
         DETACH DELETE node",
        Some(params),
    )?;
    Ok(())
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
