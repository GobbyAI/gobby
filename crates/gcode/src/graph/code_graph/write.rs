//! Code-index graph projection writes.
//!
//! This is the intentional exception to the broader "Gobby-owned stores are
//! externally managed" rule: `gcode` owns the code-index graph projection and
//! writes FalkorDB `Code*` nodes/edges derived from its PostgreSQL index rows.

use crate::config::Context;
use crate::models::{CallRelation, ImportRelation, InheritanceRelation, Symbol};
use gobby_core::falkor::GraphClient;
use serde_json::Value;
use std::collections::{BTreeSet, HashSet};

use super::connection::with_required_core_graph;

mod deletion;
mod inheritance;
mod mutation;
mod support;
mod sync_plan;

pub(crate) use deletion::{
    cleanup_orphans_queries, clear_project_query, count_file_projection_nodes_query,
    delete_content_version_queries, delete_file_graph_queries, delete_file_node_query,
    delete_stale_file_graph_queries, project_file_path_queries,
};
#[cfg(test)]
pub(crate) use deletion::{clear_all_code_index_query, project_scopes_query};
pub(in crate::graph::code_graph) use mutation::{import_graph_items, partition_call_graph_items};

use deletion::delete_bare_file_node_query;
use inheritance::partition_inheritance_graph_items;
use mutation::{SyncFileMutation, definition_graph_symbols, new_sync_token};
use support::execute_write_query;
use sync_plan::plan_sync_batches;

pub(in crate::graph::code_graph) const PROJECT_INDEXED_PROPERTIES: &[(&str, &[&str])] = &[
    ("CodeFile", &["project", "path"]),
    ("CodeSymbol", &["project", "id", "file_path"]),
    ("CodeModule", &["project", "name"]),
    ("UnresolvedCallee", &["project", "id"]),
    ("ExternalSymbol", &["project", "id"]),
];

const PROJECT_INDEXED_RELATIONSHIPS: &[(&str, &str)] = &[
    ("INHERITS", "source_file_path"),
    ("EXTENDS", "source_file_path"),
    ("IMPLEMENTS", "source_file_path"),
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

    // Eighth argument is the inheritance slice loaded by read_graph_file_facts.
    #[allow(clippy::too_many_arguments)]
    pub fn sync_file(
        &mut self,
        file_path: &str,
        content_hash: &str,
        imports: &[ImportRelation],
        definitions: &[Symbol],
        calls: &[CallRelation],
        inheritance: &[InheritanceRelation],
        cleanup_orphans: bool,
    ) -> anyhow::Result<usize> {
        let sync_token = new_sync_token(file_path);
        let import_items = import_graph_items(file_path, imports);
        let symbols = definition_graph_symbols(definitions);
        let call_groups = partition_call_graph_items(self.project_id, file_path, calls);
        let inheritance_groups =
            partition_inheritance_graph_items(self.project_id, file_path, inheritance);
        let relationship_count = import_items.len()
            + symbols.len()
            + call_groups.symbol.len()
            + call_groups.external.len()
            + call_groups.unresolved.len()
            + inheritance_groups.row_count();
        // Issue the mutation as bounded batches so no single FalkorDB request
        // grows unbounded for pathological files (gobby-cli #678).
        for query in plan_sync_batches(SyncFileMutation {
            project_id: self.project_id,
            file_path,
            content_hash,
            symbol_count: definitions.len(),
            imports: &import_items,
            symbols: &symbols,
            calls: &call_groups,
            inheritance: &inheritance_groups,
            sync_token: &sync_token,
        })? {
            execute_write_query(self.client, query)?;
        }
        // Stale delete is token-only: every current row was just written with the
        // new sync_token, so a token mismatch alone identifies stale rows — no
        // (potentially unbounded) symbol-id list is needed.
        self.delete_stale_file_graph(file_path, content_hash, &sync_token)?;
        if cleanup_orphans {
            self.cleanup_orphans()?;
        }
        Ok(relationship_count)
    }

    pub fn ensure_project_indexes(&mut self) -> anyhow::Result<()> {
        for (label, properties) in PROJECT_INDEXED_PROPERTIES {
            for property in *properties {
                self.client.ensure_exact_node_index(label, property)?;
            }
        }
        for (rel_type, property) in PROJECT_INDEXED_RELATIONSHIPS {
            self.client
                .ensure_exact_relationship_index(rel_type, property)?;
        }
        Ok(())
    }

    pub fn delete_stale_file_graph(
        &mut self,
        file_path: &str,
        content_hash: &str,
        sync_token: &str,
    ) -> anyhow::Result<()> {
        for query in
            delete_stale_file_graph_queries(self.project_id, file_path, content_hash, sync_token)?
        {
            execute_write_query(self.client, query)?;
        }
        Ok(())
    }

    pub(crate) fn delete_content_version(
        &mut self,
        file_path: &str,
        content_hash: &str,
    ) -> anyhow::Result<()> {
        for query in delete_content_version_queries(self.project_id, file_path, content_hash)? {
            execute_write_query(self.client, query)?;
        }
        self.cleanup_orphans()
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

    /// Reconcile a file that currently has no graph facts: sweep this content
    /// hash's stale rows without MERGE-ing a file node, then drop the file node
    /// only if nothing anchors to it any more. Facts written under another
    /// content hash (other machines' views) survive.
    pub fn sync_no_fact_file(&mut self, file_path: &str, content_hash: &str) -> anyhow::Result<()> {
        let sync_token = new_sync_token(file_path);
        self.delete_stale_file_graph(file_path, content_hash, &sync_token)?;
        execute_write_query(
            self.client,
            delete_bare_file_node_query(self.project_id, file_path)?,
        )
    }

    pub fn delete_file_node(&mut self, file_path: &str) -> anyhow::Result<()> {
        execute_write_query(
            self.client,
            delete_file_node_query(self.project_id, file_path)?,
        )
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

#[allow(clippy::too_many_arguments)]
pub fn sync_file_graph(
    ctx: &Context,
    file_path: &str,
    content_hash: &str,
    imports: &[ImportRelation],
    definitions: &[Symbol],
    calls: &[CallRelation],
    inheritance: &[InheritanceRelation],
    cleanup_orphans: bool,
) -> anyhow::Result<usize> {
    with_code_graph(ctx, |graph| {
        graph.sync_file(
            file_path,
            content_hash,
            imports,
            definitions,
            calls,
            inheritance,
            cleanup_orphans,
        )
    })
}

pub fn sync_no_fact_file(ctx: &Context, file_path: &str, content_hash: &str) -> anyhow::Result<()> {
    with_code_graph(ctx, |graph| {
        graph.sync_no_fact_file(file_path, content_hash)
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

pub(crate) fn delete_content_version(
    ctx: &Context,
    file_path: &str,
    content_hash: &str,
) -> anyhow::Result<()> {
    with_required_core_graph(ctx, |client| {
        CodeGraph::new(&ctx.project_id, client).delete_content_version(file_path, content_hash)
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

#[cfg(test)]
pub(in crate::graph::code_graph) fn plan_test_sync_file(
    project_id: &str,
    file_path: &str,
    content_hash: &str,
    inheritance: &[crate::models::InheritanceRelation],
    sync_token: &str,
) -> anyhow::Result<Vec<crate::graph::typed_query::TypedQuery>> {
    let calls = partition_call_graph_items(project_id, file_path, &[]);
    let inheritance_groups = partition_inheritance_graph_items(project_id, file_path, inheritance);
    plan_sync_batches(SyncFileMutation {
        project_id,
        file_path,
        content_hash,
        symbol_count: 0,
        imports: &[],
        symbols: &[],
        calls: &calls,
        inheritance: &inheritance_groups,
        sync_token,
    })
}
