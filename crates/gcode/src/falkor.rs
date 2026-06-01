//! Compatibility facade for FalkorDB graph queries.
//!
//! The reusable projection/query implementation lives under
//! `crate::graph::code_graph`; this module keeps the Phase 7 public surface
//! available for downstream callers that still import `crate::falkor`.

use std::collections::HashMap;

use gobby_core::falkor::GraphClient;

use crate::config::{Context, FalkorConfig};
use crate::graph::typed_query;
use crate::models::GraphResult;

const MAX_GRAPH_LIMIT: usize = 100;

/// Row from a FalkorDB query response.
pub type Row = gobby_core::falkor::Row;

/// Blocking FalkorDB graph client.
pub struct FalkorClient {
    client: GraphClient,
}

impl FalkorClient {
    pub fn from_config(config: &FalkorConfig) -> anyhow::Result<Self> {
        let connection_config = config.connection_config();
        let client = GraphClient::from_config(&connection_config, &config.graph_name)?;
        Ok(Self { client })
    }

    /// Execute a Cypher query and return parsed rows.
    pub fn query(
        &mut self,
        cypher: &str,
        params: Option<HashMap<String, String>>,
    ) -> anyhow::Result<Vec<Row>> {
        self.client.query(cypher, params)
    }

    /// Execute a typed query after its parameters have been rendered safely.
    pub fn query_typed(&mut self, query: typed_query::TypedQuery) -> anyhow::Result<Vec<Row>> {
        let typed_query::TypedQuery { cypher, params } = query;
        self.query(&cypher, Some(params))
    }

    pub fn with_core_client<T>(
        &mut self,
        f: impl FnOnce(&mut GraphClient) -> anyhow::Result<T>,
    ) -> anyhow::Result<T> {
        f(&mut self.client)
    }
}

pub fn cypher_string_literal(s: &str) -> String {
    crate::graph::typed_query::cypher_string_literal(s)
}

pub fn id_list_literal(ids: &[String]) -> String {
    typed_query::id_list_literal(ids)
}

/// Bound user-supplied offsets to the graph query page cap.
///
/// Callers may pass any non-negative offset, but FalkorDB reads stay capped at
/// `MAX_GRAPH_LIMIT` so accidental deep pages cannot trigger unbounded scans.
pub fn clamp_offset(offset: usize) -> usize {
    offset.min(MAX_GRAPH_LIMIT)
}

pub fn count_callers_query(project_id: &str, symbol_id: &str) -> (String, HashMap<String, String>) {
    crate::graph::code_graph::count_callers_query(project_id, symbol_id)
}

pub fn count_usages_query(project_id: &str, symbol_id: &str) -> (String, HashMap<String, String>) {
    crate::graph::code_graph::count_usages_query(project_id, symbol_id)
}

pub fn find_callers_query(
    project_id: &str,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> (String, HashMap<String, String>) {
    crate::graph::code_graph::find_callers_query(project_id, symbol_id, offset, limit)
}

pub fn find_usages_query(
    project_id: &str,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> (String, HashMap<String, String>) {
    crate::graph::code_graph::find_usages_query(project_id, symbol_id, offset, limit)
}

pub fn find_callers_batch_query(
    project_id: &str,
    symbol_ids: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    crate::graph::code_graph::find_callers_batch_query(project_id, symbol_ids, limit)
}

pub fn find_callees_batch_query(
    project_id: &str,
    symbol_ids: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    crate::graph::code_graph::find_callees_batch_query(project_id, symbol_ids, limit)
}

pub fn get_imports_query(project_id: &str, file_path: &str) -> (String, HashMap<String, String>) {
    crate::graph::code_graph::get_imports_query(project_id, file_path)
}

pub fn blast_radius_query(depth: usize, limit: usize) -> String {
    crate::graph::code_graph::blast_radius_query(depth, limit)
}

pub fn with_falkor<T>(
    ctx: &Context,
    default: T,
    f: impl FnOnce(&mut FalkorClient) -> anyhow::Result<T>,
) -> anyhow::Result<T> {
    let Some(config) = &ctx.falkordb else {
        return Ok(default);
    };

    let mut client = match FalkorClient::from_config(config) {
        Ok(client) => client,
        Err(e) => {
            if !ctx.quiet {
                eprintln!("Warning: FalkorDB connection failed: {e}");
            }
            return Ok(default);
        }
    };

    match f(&mut client) {
        Ok(value) => Ok(value),
        Err(e) => {
            if !ctx.quiet {
                eprintln!("Warning: FalkorDB query failed: {e}");
            }
            Ok(default)
        }
    }
}

/// Count callers of a symbol.
pub fn count_callers(ctx: &Context, symbol_id: &str) -> anyhow::Result<usize> {
    crate::graph::code_graph::count_callers(ctx, symbol_id)
}

/// Count incoming call usages of a symbol.
pub fn count_usages(ctx: &Context, symbol_id: &str) -> anyhow::Result<usize> {
    crate::graph::code_graph::count_usages(ctx, symbol_id)
}

/// Find symbols that call the given symbol id.
pub fn find_callers(
    ctx: &Context,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    crate::graph::code_graph::find_callers(ctx, symbol_id, offset, limit)
}

/// Find incoming CALLS usages for a canonical, unresolved, or external target.
pub fn find_usages(
    ctx: &Context,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    crate::graph::code_graph::find_usages(ctx, symbol_id, offset, limit)
}

/// Find symbols that call any of the given target ids.
pub fn find_callers_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<HashMap<String, Vec<GraphResult>>> {
    let mut grouped = HashMap::new();
    for symbol_id in symbol_ids {
        grouped.insert(
            symbol_id.clone(),
            crate::graph::code_graph::find_callers(ctx, symbol_id, 0, limit)?,
        );
    }
    Ok(grouped)
}

/// Find call targets reached by any of the given source ids.
pub fn find_callees_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<HashMap<String, Vec<GraphResult>>> {
    let mut grouped = HashMap::new();
    for symbol_id in symbol_ids {
        grouped.insert(
            symbol_id.clone(),
            crate::graph::code_graph::find_callees_batch(
                ctx,
                std::slice::from_ref(symbol_id),
                limit,
            )?,
        );
    }
    Ok(grouped)
}

/// Get import graph for a file.
pub fn get_imports(ctx: &Context, file_path: &str) -> anyhow::Result<Vec<GraphResult>> {
    crate::graph::code_graph::get_imports(ctx, file_path)
}

/// Find transitive blast radius of changing a symbol.
pub fn blast_radius(
    ctx: &Context,
    symbol_id: &str,
    depth: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    crate::graph::code_graph::blast_radius(ctx, symbol_id, depth)
}

#[cfg(test)]
fn row_to_graph_result(row: &Row) -> GraphResult {
    crate::graph::code_graph::row_to_graph_result(row)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const CALL_TARGET_FRAGMENT: &str =
        "target:CodeSymbol OR target:UnresolvedCallee OR target:ExternalSymbol";

    fn assert_no_numeric_or_list_placeholders(query: &str) {
        assert!(!query.contains("$offset"), "{query}");
        assert!(!query.contains("$limit"), "{query}");
        assert!(!query.contains("$ids"), "{query}");
    }

    #[test]
    fn cypher_string_literal_escapes_single_quotes_and_backslashes() {
        assert_eq!(
            cypher_string_literal("module\\path'symbol"),
            "'module\\\\path\\'symbol'"
        );
    }

    #[test]
    fn find_callers_query_interpolates_numeric_skip_and_limit() {
        let (query, params) = find_callers_query("project-1", "symbol-1", 250, 0);

        assert!(query.contains("SKIP 100 LIMIT 1"), "{query}");
        assert_no_numeric_or_list_placeholders(&query);
        assert_eq!(
            params.get("project").map(String::as_str),
            Some("'project-1'")
        );
        assert_eq!(params.get("id").map(String::as_str), Some("'symbol-1'"));
    }

    #[test]
    fn find_usages_query_clamps_numeric_skip_and_limit() {
        let (query, params) = find_usages_query("project-1", "symbol-1", 250, 250);

        assert!(query.contains("SKIP 100 LIMIT 100"), "{query}");
        assert_no_numeric_or_list_placeholders(&query);
        assert_eq!(
            params.get("project").map(String::as_str),
            Some("'project-1'")
        );
        assert_eq!(params.get("id").map(String::as_str), Some("'symbol-1'"));
    }

    #[test]
    fn batch_query_uses_one_interpolated_in_list() {
        let (query, params) =
            find_callers_batch_query("project-1", &["a".to_string(), "b'\\c".to_string()], 250);

        assert_eq!(query.matches(" IN [").count(), 1, "{query}");
        assert!(query.contains("target.id IN ['a', 'b\\'\\\\c']"), "{query}");
        assert!(query.contains("LIMIT 100"), "{query}");
        assert_no_numeric_or_list_placeholders(&query);
        assert_eq!(
            params.get("project").map(String::as_str),
            Some("'project-1'")
        );
    }

    #[test]
    fn blast_radius_query_clamps_depth_and_interpolates_limit() {
        let query = blast_radius_query(99, 250);

        assert!(query.contains(CALL_TARGET_FRAGMENT), "{query}");
        assert!(query.contains("[:CALLS*1..5]"), "{query}");
        assert!(query.contains("LIMIT 100"), "{query}");
        assert_no_numeric_or_list_placeholders(&query);
    }

    #[test]
    fn row_to_graph_result_prefers_blast_radius_node_fields() {
        let row = Row::from([
            ("node_id".to_string(), json!("sym-1")),
            ("node_name".to_string(), json!("foo")),
            ("file_path".to_string(), json!("src/main.py")),
            ("line".to_string(), json!(42)),
            ("rel_type".to_string(), json!("call")),
            ("distance".to_string(), json!(2)),
        ]);

        let result = row_to_graph_result(&row);

        assert_eq!(result.id, "sym-1");
        assert_eq!(result.name, "foo");
        assert_eq!(result.file_path, "src/main.py");
        assert_eq!(result.line, 42);
        assert_eq!(result.relation.as_deref(), Some("call"));
        assert_eq!(result.distance, Some(2));
    }

    #[test]
    fn phase7_query_helpers_preserve_safe_literals_clamping_and_project_scope() {
        let project_id = "project\n'one";
        let symbol_id = "symbol\"\\'two";
        let expected_project = cypher_string_literal(project_id);
        let expected_symbol = cypher_string_literal(symbol_id);

        let (callers, caller_params) = find_callers_query(project_id, symbol_id, 250, 0);
        assert!(callers.contains(CALL_TARGET_FRAGMENT), "{callers}");
        assert!(callers.contains("SKIP 100 LIMIT 1"), "{callers}");
        assert_no_numeric_or_list_placeholders(&callers);
        assert_eq!(caller_params.get("project"), Some(&expected_project));
        assert_eq!(caller_params.get("id"), Some(&expected_symbol));

        let (usages, usage_params) = find_usages_query(project_id, symbol_id, 250, 250);
        assert!(usages.contains(CALL_TARGET_FRAGMENT), "{usages}");
        assert!(usages.contains("SKIP 100 LIMIT 100"), "{usages}");
        assert_no_numeric_or_list_placeholders(&usages);
        assert_eq!(usage_params.get("project"), Some(&expected_project));
        assert_eq!(usage_params.get("id"), Some(&expected_symbol));

        let ids = ["a".to_string(), "b\n\"'\\c".to_string()];
        let expected_ids = id_list_literal(&ids);
        let (batch, batch_params) = find_callers_batch_query(project_id, &ids, 250);
        assert!(
            batch.contains(&format!("target.id IN [{expected_ids}]")),
            "{batch}"
        );
        assert!(batch.contains("LIMIT 100"), "{batch}");
        assert_no_numeric_or_list_placeholders(&batch);
        assert_eq!(batch_params.get("project"), Some(&expected_project));

        let blast = blast_radius_query(99, 250);
        assert!(blast.contains(CALL_TARGET_FRAGMENT), "{blast}");
        assert!(blast.contains("[:CALLS*1..5]"), "{blast}");
        assert!(blast.contains("LIMIT 100"), "{blast}");
        assert_no_numeric_or_list_placeholders(&blast);
    }
}
