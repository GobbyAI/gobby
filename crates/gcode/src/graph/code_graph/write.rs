//! Code-index graph projection writes.
//!
//! This is the intentional exception to the broader "Gobby-owned stores are
//! externally managed" rule: `gcode` owns the code-index graph projection and
//! writes FalkorDB `Code*` nodes/edges derived from its PostgreSQL index rows.

use crate::config::Context;
use crate::models::{CallRelation, ImportRelation, Symbol};
use gobby_core::degradation::ServiceState;
use gobby_core::falkor::GraphClient;
use serde_json::Value;
use std::collections::{BTreeSet, HashSet};

use super::GraphReadError;
use super::connection::with_required_core_graph;

mod deletion;
mod mutation;
mod support;
mod sync_plan;

pub(crate) use deletion::{
    cleanup_orphans_queries, clear_all_code_index_query, clear_project_query,
    count_file_projection_nodes_query, delete_file_graph_queries, delete_file_node_query,
    project_file_path_queries,
};
pub use mutation::call_target_id;
pub(in crate::graph::code_graph) use mutation::{import_graph_items, partition_call_graph_items};

use deletion::delete_stale_file_graph_queries;
use mutation::{
    SyncFileMutation, add_definitions_query, add_external_calls_query, add_imports_query,
    add_symbol_calls_query, add_unresolved_calls_query, definition_graph_symbols,
    ensure_file_node_query, new_sync_token,
};
use support::execute_write_query;
use sync_plan::plan_sync_batches;

const PROJECT_INDEXED_LABELS: &[&str] = &[
    "CodeFile",
    "CodeSymbol",
    "CodeModule",
    "UnresolvedCallee",
    "ExternalSymbol",
];

pub struct CodeGraph<'a> {
    project_id: &'a str,
    client: &'a mut GraphClient,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GraphOrphanCleanup {
    pub stale_files_deleted: usize,
    pub graph_nodes_deleted: usize,
}

impl<'a> CodeGraph<'a> {
    pub fn new(project_id: &'a str, client: &'a mut GraphClient) -> Self {
        Self { project_id, client }
    }

    pub fn sync_file(
        &mut self,
        file_path: &str,
        imports: &[ImportRelation],
        definitions: &[Symbol],
        calls: &[CallRelation],
        cleanup_orphans: bool,
    ) -> anyhow::Result<usize> {
        let sync_token = new_sync_token(file_path);
        let import_items = import_graph_items(file_path, imports);
        let symbols = definition_graph_symbols(definitions);
        let call_groups = partition_call_graph_items(self.project_id, file_path, calls);
        let relationship_count = import_items.len()
            + symbols.len()
            + call_groups.symbol.len()
            + call_groups.external.len()
            + call_groups.unresolved.len();
        // Issue the mutation as bounded batches so no single FalkorDB request
        // grows unbounded for pathological files (gobby-cli #678).
        for query in plan_sync_batches(SyncFileMutation {
            project_id: self.project_id,
            file_path,
            symbol_count: definitions.len(),
            imports: &import_items,
            symbols: &symbols,
            calls: &call_groups,
            sync_token: &sync_token,
        })? {
            execute_write_query(self.client, query)?;
        }
        // Stale delete is token-only: every current row was just written with the
        // new sync_token, so a token mismatch alone identifies stale rows — no
        // (potentially unbounded) symbol-id list is needed.
        self.delete_stale_file_graph(file_path, &sync_token)?;
        if cleanup_orphans {
            self.cleanup_orphans()?;
        }
        Ok(relationship_count)
    }

    pub fn ensure_project_indexes(&mut self) -> anyhow::Result<()> {
        for label in PROJECT_INDEXED_LABELS {
            self.client.ensure_exact_node_index(label, "project")?;
        }
        Ok(())
    }

    pub fn ensure_file_node(
        &mut self,
        file_path: &str,
        symbol_count: usize,
        sync_token: &str,
    ) -> anyhow::Result<()> {
        execute_write_query(
            self.client,
            ensure_file_node_query(self.project_id, file_path, symbol_count, sync_token)?,
        )
    }

    pub fn add_imports(
        &mut self,
        file_path: &str,
        imports: &[ImportRelation],
        sync_token: &str,
    ) -> anyhow::Result<usize> {
        let items = import_graph_items(file_path, imports);
        if items.is_empty() {
            return Ok(0);
        }
        let written = items.len();
        execute_write_query(
            self.client,
            add_imports_query(self.project_id, &items, sync_token)?,
        )?;
        Ok(written)
    }

    pub fn add_definitions(
        &mut self,
        file_path: &str,
        definitions: &[Symbol],
        sync_token: &str,
    ) -> anyhow::Result<usize> {
        let symbols = definitions
            .iter()
            .filter(|symbol| !symbol.id.is_empty() && !symbol.name.is_empty())
            .collect::<Vec<_>>();
        if symbols.is_empty() {
            return Ok(0);
        }
        let written = symbols.len();
        execute_write_query(
            self.client,
            add_definitions_query(self.project_id, file_path, &symbols, sync_token)?,
        )?;
        Ok(written)
    }

    pub fn add_calls(
        &mut self,
        file_path: &str,
        calls: &[CallRelation],
        sync_token: &str,
    ) -> anyhow::Result<usize> {
        let call_groups = partition_call_graph_items(self.project_id, file_path, calls);

        let mut written = 0;
        if !call_groups.symbol.is_empty() {
            written += call_groups.symbol.len();
            execute_write_query(
                self.client,
                add_symbol_calls_query(self.project_id, &call_groups.symbol, sync_token)?,
            )?;
        }
        if !call_groups.external.is_empty() {
            written += call_groups.external.len();
            execute_write_query(
                self.client,
                add_external_calls_query(self.project_id, &call_groups.external, sync_token)?,
            )?;
        }
        if !call_groups.unresolved.is_empty() {
            written += call_groups.unresolved.len();
            execute_write_query(
                self.client,
                add_unresolved_calls_query(self.project_id, &call_groups.unresolved, sync_token)?,
            )?;
        }
        Ok(written)
    }

    pub fn delete_stale_file_graph(
        &mut self,
        file_path: &str,
        sync_token: &str,
    ) -> anyhow::Result<()> {
        for query in delete_stale_file_graph_queries(self.project_id, file_path, sync_token)? {
            execute_write_query(self.client, query)?;
        }
        Ok(())
    }

    pub fn delete_file_graph(
        &mut self,
        file_path: &str,
        current_symbol_ids: &[String],
    ) -> anyhow::Result<()> {
        for query in delete_file_graph_queries(self.project_id, file_path, current_symbol_ids)? {
            execute_write_query(self.client, query)?;
        }
        Ok(())
    }

    pub fn delete_file_node(&mut self, file_path: &str) -> anyhow::Result<()> {
        execute_write_query(
            self.client,
            delete_file_node_query(self.project_id, file_path)?,
        )
    }

    pub fn delete_file_projection(&mut self, file_path: &str) -> anyhow::Result<()> {
        self.delete_file_graph(file_path, &[])?;
        self.delete_file_node(file_path)?;
        self.cleanup_orphans()
    }

    pub fn cleanup_orphans(&mut self) -> anyhow::Result<()> {
        for query in cleanup_orphans_queries(self.project_id)? {
            execute_write_query(self.client, query)?;
        }
        Ok(())
    }

    pub fn cleanup_deleted_files(
        &mut self,
        indexed_file_paths: &HashSet<String>,
    ) -> anyhow::Result<GraphOrphanCleanup> {
        let graph_file_paths = self.project_file_paths()?;
        let stale_file_paths = graph_file_paths
            .into_iter()
            .filter(|file_path| !indexed_file_paths.contains(file_path))
            .collect::<Vec<_>>();
        let mut graph_nodes_deleted = 0;

        for file_path in &stale_file_paths {
            graph_nodes_deleted += self.count_file_projection_nodes(file_path)?;
            self.delete_file_graph(file_path, &[])?;
            self.delete_file_node(file_path)?;
        }

        self.cleanup_orphans()?;
        Ok(GraphOrphanCleanup {
            stale_files_deleted: stale_file_paths.len(),
            graph_nodes_deleted,
        })
    }

    fn project_file_paths(&mut self) -> anyhow::Result<BTreeSet<String>> {
        let mut file_paths = BTreeSet::new();
        for query in project_file_path_queries(self.project_id)? {
            let crate::graph::typed_query::TypedQuery { cypher, params } = query;
            for row in self.client.query(&cypher, Some(params))? {
                if let Some(file_path) = row.get("path").and_then(Value::as_str) {
                    file_paths.insert(file_path.to_string());
                }
            }
        }
        Ok(file_paths)
    }

    fn count_file_projection_nodes(&mut self, file_path: &str) -> anyhow::Result<usize> {
        let query = count_file_projection_nodes_query(self.project_id, file_path)?;
        let crate::graph::typed_query::TypedQuery { cypher, params } = query;
        let rows = self.client.query(&cypher, Some(params))?;
        Ok(rows
            .first()
            .and_then(|row| row.get("nodes"))
            .and_then(value_to_usize)
            .unwrap_or(0))
    }

    pub fn clear_project(&mut self) -> anyhow::Result<()> {
        execute_write_query(self.client, clear_project_query(self.project_id)?)
    }
}

fn value_to_usize(value: &Value) -> Option<usize> {
    if let Some(value) = value.as_u64() {
        return usize::try_from(value).ok();
    }
    value.as_i64().and_then(|value| usize::try_from(value).ok())
}

pub fn sync_file_graph(
    ctx: &Context,
    file_path: &str,
    imports: &[ImportRelation],
    definitions: &[Symbol],
    calls: &[CallRelation],
    cleanup_orphans: bool,
) -> anyhow::Result<usize> {
    with_code_graph(ctx, |graph| {
        graph.sync_file(file_path, imports, definitions, calls, cleanup_orphans)
    })
}

pub fn with_code_graph<T>(
    ctx: &Context,
    f: impl FnOnce(&mut CodeGraph<'_>) -> anyhow::Result<T>,
) -> anyhow::Result<T> {
    with_required_core_graph(ctx, |client| {
        let mut graph = CodeGraph::new(&ctx.project_id, client);
        graph.ensure_project_indexes()?;
        f(&mut graph)
    })
}

pub fn delete_file_graph(
    ctx: &Context,
    file_path: &str,
    current_symbol_ids: &[String],
) -> anyhow::Result<()> {
    with_required_core_graph(ctx, |client| {
        CodeGraph::new(&ctx.project_id, client).delete_file_graph(file_path, current_symbol_ids)
    })
}

pub fn delete_file_projection(ctx: &Context, file_path: &str) -> anyhow::Result<()> {
    with_required_core_graph(ctx, |client| {
        CodeGraph::new(&ctx.project_id, client).delete_file_projection(file_path)
    })
}

pub fn cleanup_orphans(ctx: &Context) -> anyhow::Result<()> {
    with_code_graph(ctx, |graph| graph.cleanup_orphans())
}

pub fn cleanup_deleted_files(
    ctx: &Context,
    indexed_file_paths: &HashSet<String>,
) -> anyhow::Result<GraphOrphanCleanup> {
    with_code_graph(ctx, |graph| graph.cleanup_deleted_files(indexed_file_paths))
}

pub fn clear_project(ctx: &Context) -> anyhow::Result<()> {
    with_required_core_graph(ctx, |client| {
        CodeGraph::new(&ctx.project_id, client).clear_project()
    })
}

pub fn clear_all_code_index(config: &crate::config::FalkorConfig) -> anyhow::Result<()> {
    let connection_config = config.connection_config();
    match gobby_core::falkor::with_graph(
        Some(&connection_config),
        &config.graph_name,
        None,
        |client| execute_write_query(client, clear_all_code_index_query()?).map(Some),
    ) {
        Ok((Some(()), ServiceState::Available)) => Ok(()),
        Ok((_, ServiceState::NotConfigured)) => Err(GraphReadError::NotConfigured.into()),
        Ok((_, ServiceState::Unreachable { message })) => {
            log::warn!("FalkorDB was unreachable while clearing code graph: {message}");
            Err(GraphReadError::Unreachable { message }.into())
        }
        Ok((None, ServiceState::Available)) => Err(GraphReadError::QueryFailed {
            message: "graph clear returned no value".to_string(),
        }
        .into()),
        Err(error) => Err(GraphReadError::QueryFailed {
            message: format!("{error:#}"),
        }
        .into()),
    }
}
