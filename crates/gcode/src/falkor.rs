//! Compatibility facade for FalkorDB graph queries.
//!
//! The reusable projection/query implementation lives under
//! `crate::graph::code_graph`; this module keeps the Phase 7 public surface
//! available for downstream callers that still import `crate::falkor`.

use std::collections::HashMap;

use falkordb::{
    FalkorClientBuilder, FalkorConnectionInfo, FalkorValue, LazyResultSet, QueryResult, SyncGraph,
};
use serde_json::{Map, Number, Value};

use crate::config::{Context, FalkorConfig};
use crate::graph::typed_query;
use crate::models::GraphResult;

const CALL_TARGET_PREDICATE: &str =
    "target:CodeSymbol OR target:UnresolvedCallee OR target:ExternalSymbol";
const MAX_GRAPH_LIMIT: usize = 100;

/// Row from a FalkorDB query response.
pub type Row = HashMap<String, Value>;

/// Blocking FalkorDB graph client.
pub struct FalkorClient {
    graph: SyncGraph,
}

impl FalkorClient {
    pub fn from_config(config: &FalkorConfig) -> anyhow::Result<Self> {
        let password = config.password.as_deref().unwrap_or_default();
        let url = format!(
            "falkor://:{}@{}:{}",
            urlencoding::encode(password),
            config.host,
            config.port
        );
        let conn_info: FalkorConnectionInfo = url.as_str().try_into()?;
        let client = FalkorClientBuilder::new()
            .with_connection_info(conn_info)
            .build()?;
        Ok(Self {
            graph: client.select_graph(&config.graph_name),
        })
    }

    /// Execute a Cypher query and return parsed rows.
    pub fn query(
        &mut self,
        cypher: &str,
        params: Option<HashMap<String, String>>,
    ) -> anyhow::Result<Vec<Row>> {
        match params {
            Some(params) => {
                let result = self.graph.query(cypher).with_params(&params).execute()?;
                Ok(parse_falkor_result(result))
            }
            None => {
                let result = self.graph.query(cypher).execute()?;
                Ok(parse_falkor_result(result))
            }
        }
    }

    /// Execute a typed query after its parameters have been rendered safely.
    pub fn query_typed(&mut self, query: typed_query::TypedQuery) -> anyhow::Result<Vec<Row>> {
        let typed_query::TypedQuery { cypher, params } = query;
        self.query(&cypher, Some(params))
    }
}

pub fn cypher_string_literal(s: &str) -> String {
    crate::graph::typed_query::cypher_string_literal(s)
}

pub fn id_list_literal(ids: &[String]) -> String {
    typed_query::id_list_literal(ids)
}

fn clamp_limit(limit: usize) -> usize {
    limit.clamp(1, MAX_GRAPH_LIMIT)
}

pub fn clamp_offset(offset: usize) -> usize {
    offset.min(MAX_GRAPH_LIMIT)
}

pub fn count_callers_query(project_id: &str, symbol_id: &str) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (caller:CodeSymbol {{project: $project}})-[:CALLS]->(target {{id: $id, project: $project}}) \
             WHERE {CALL_TARGET_PREDICATE} \
             RETURN count(caller) AS cnt"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub fn count_usages_query(project_id: &str, symbol_id: &str) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (source:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{id: $id, project: $project}}) \
             WHERE {CALL_TARGET_PREDICATE} \
             RETURN count(source) AS cnt"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub fn find_callers_query(
    project_id: &str,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let offset = clamp_offset(offset);
    let limit = clamp_limit(limit);
    (
        format!(
            "MATCH (caller:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{id: $id, project: $project}}) \
             WHERE {CALL_TARGET_PREDICATE} \
             RETURN caller.id AS caller_id, caller.name AS caller_name, \
                    r.file AS file, r.line AS line \
             SKIP {offset} LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub fn find_usages_query(
    project_id: &str,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let offset = clamp_offset(offset);
    let limit = clamp_limit(limit);
    (
        format!(
            "MATCH (source:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{id: $id, project: $project}}) \
             WHERE {CALL_TARGET_PREDICATE} \
             RETURN source.id AS source_id, source.name AS source_name, \
                    'CALLS' AS rel_type, r.file AS file, r.line AS line \
             SKIP {offset} LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub fn find_callers_batch_query(
    project_id: &str,
    symbol_ids: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let ids = id_list_literal(symbol_ids);
    (
        format!(
            "MATCH (caller:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{project: $project}}) \
             WHERE ({CALL_TARGET_PREDICATE}) AND target.id IN [{ids}] \
             RETURN caller.id AS caller_id, caller.name AS caller_name, \
                    r.file AS file, r.line AS line \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub fn find_callees_batch_query(
    project_id: &str,
    symbol_ids: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let ids = id_list_literal(symbol_ids);
    (
        format!(
            "MATCH (src:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{project: $project}}) \
             WHERE src.id IN [{ids}] AND ({CALL_TARGET_PREDICATE}) \
             RETURN target.id AS callee_id, target.name AS callee_name, \
                    r.file AS file, r.line AS line \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub fn get_imports_query(project_id: &str, file_path: &str) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(MAX_GRAPH_LIMIT);
    (
        format!(
            "MATCH (f:CodeFile {{path: $path, project: $project}})-[:IMPORTS]->(m:CodeModule) \
             RETURN m.name AS module_name \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("path", file_path)]),
    )
}

pub fn blast_radius_query(depth: usize, limit: usize) -> String {
    let depth = depth.clamp(1, 5);
    let limit = limit.clamp(1, MAX_GRAPH_LIMIT);
    format!(
        "MATCH (target {{id: $id, project: $project}}) \
         WHERE {CALL_TARGET_PREDICATE} \
         MATCH path = (affected:CodeSymbol {{project: $project}})-[:CALLS*1..{depth}]->(target) \
         WITH affected, min(length(path)) AS distance \
         OPTIONAL MATCH (file:CodeFile {{project: $project}})-[:DEFINES]->(affected) \
         RETURN DISTINCT affected.id AS node_id, \
                affected.name AS node_name, \
                affected.kind AS kind, file.path AS file_path, \
                affected.line_start AS line, \
                distance, 'call' AS rel_type \
         ORDER BY distance ASC, affected.name ASC \
         LIMIT {limit}"
    )
}

fn parse_falkor_result(result: QueryResult<LazyResultSet<'_>>) -> Vec<Row> {
    parse_falkor_records(result.header, result.data)
}

fn parse_falkor_records<I>(headers: Vec<String>, records: I) -> Vec<Row>
where
    I: IntoIterator<Item = Vec<FalkorValue>>,
{
    records
        .into_iter()
        .map(|record| {
            let mut row = HashMap::new();
            for (i, field) in headers.iter().enumerate() {
                let value = record.get(i).cloned().unwrap_or(FalkorValue::None);
                row.insert(field.clone(), falkor_value_to_json(value));
            }
            row
        })
        .collect()
}

fn falkor_value_to_json(value: FalkorValue) -> Value {
    match value {
        FalkorValue::String(value) => Value::String(value),
        FalkorValue::Bool(value) => Value::Bool(value),
        FalkorValue::I64(value) => Value::Number(Number::from(value)),
        FalkorValue::F64(value) => Number::from_f64(value)
            .map(Value::Number)
            .unwrap_or(Value::Null),
        FalkorValue::Array(values) => Value::Array(
            values
                .into_iter()
                .map(falkor_value_to_json)
                .collect::<Vec<_>>(),
        ),
        FalkorValue::Map(values) => Value::Object(
            values
                .into_iter()
                .map(|(key, value)| (key, falkor_value_to_json(value)))
                .collect::<Map<_, _>>(),
        ),
        FalkorValue::None => Value::Null,
        value => Value::String(format!("{value:?}")),
    }
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
    use falkordb::FalkorValue;
    use serde_json::json;

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

        assert!(query.contains(CALL_TARGET_PREDICATE), "{query}");
        assert!(query.contains("[:CALLS*1..5]"), "{query}");
        assert!(query.contains("LIMIT 100"), "{query}");
        assert_no_numeric_or_list_placeholders(&query);
    }

    #[test]
    fn convert_falkor_records_maps_headers_and_row_values() {
        let headers = vec!["name".to_string(), "age".to_string(), "empty".to_string()];
        let rows = vec![vec![
            FalkorValue::String("Alice".to_string()),
            FalkorValue::I64(30),
            FalkorValue::None,
        ]];

        let parsed = parse_falkor_records(headers, rows);

        assert_eq!(parsed.len(), 1);
        assert_eq!(parsed[0].get("name"), Some(&json!("Alice")));
        assert_eq!(parsed[0].get("age"), Some(&json!(30)));
        assert_eq!(parsed[0].get("empty"), Some(&json!(null)));
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
    fn falkor_client_wrapper_shape() {
        let source = include_str!("falkor.rs");
        assert!(source.contains("pub struct FalkorClient"));
        assert!(source.contains("graph: SyncGraph"));
        assert!(
            source.contains("pub fn from_config(config: &FalkorConfig) -> anyhow::Result<Self>")
        );
        assert!(source.contains("pub fn with_falkor<T>"));
        assert!(source.contains("FalkorClientBuilder, FalkorConnectionInfo, FalkorValue, LazyResultSet, QueryResult, SyncGraph"));
        assert!(source.contains("client.select_graph(&config.graph_name)"));
    }

    #[test]
    fn phase7_read_helpers_visible() {
        let source = include_str!("falkor.rs");
        for symbol in [
            "pub fn count_callers(",
            "pub fn count_usages(",
            "pub fn find_callers(",
            "pub fn find_usages(",
            "pub fn find_callers_batch(",
            "pub fn find_callees_batch(",
            "pub fn get_imports(",
            "pub fn blast_radius(",
            "pub fn count_callers_query(",
            "pub fn count_usages_query(",
            "pub fn find_callers_query(",
            "pub fn find_usages_query(",
            "pub fn find_callers_batch_query(",
            "pub fn find_callees_batch_query(",
            "pub fn get_imports_query(",
            "fn blast_radius_query(depth: usize, limit: usize)",
        ] {
            assert!(source.contains(symbol), "missing {symbol}");
        }
    }

    #[test]
    fn phase7_source_fragments_visible() {
        let source = include_str!("falkor.rs");
        for fragment in [
            "urlencoding::encode(password)",
            "falkor://:{}@{}:{}",
            ".with_connection_info(conn_info)",
            ".with_params(&",
            "result.header",
            "FalkorValue::None",
            "let mut client =",
            "ctx.falkordb",
        ] {
            assert!(source.contains(fragment), "missing {fragment}");
        }
    }

    #[test]
    fn phase7_query_surface_visible() {
        let source = include_str!("falkor.rs");
        assert!(source.contains("pub type Row = HashMap<String, Value>"));
        assert!(source.contains("pub fn query("));
        assert!(source.contains("cypher: &str"));
        assert!(source.contains("params: Option<HashMap<String, String>>"));
        assert!(source.contains("anyhow::Result<Vec<Row>>"));
        assert!(source.contains("fn parse_falkor_result("));
    }

    #[test]
    fn phase7_query_helpers_and_literal_fragments_visible() {
        let source = include_str!("falkor.rs");
        for fragment in [
            "pub fn cypher_string_literal",
            "pub fn id_list_literal",
            "pub fn clamp_offset",
            "target:CodeSymbol OR target:UnresolvedCallee OR target:ExternalSymbol",
            "SKIP {offset} LIMIT {limit}",
            "target.id IN [{ids}]",
        ] {
            assert!(source.contains(fragment), "missing {fragment}");
        }

        let queries = [
            find_callers_query("project-1", "symbol-1", 5, 10).0,
            find_usages_query("project-1", "symbol-1", 5, 10).0,
            find_callers_batch_query("project-1", &["a".to_string()], 10).0,
            find_callees_batch_query("project-1", &["a".to_string()], 10).0,
        ];
        for query in queries {
            assert_no_numeric_or_list_placeholders(&query);
        }
    }

    #[test]
    fn phase7_cargo_and_lockfile_state() {
        let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        let cargo = std::fs::read_to_string(manifest_dir.join("Cargo.toml"))
            .expect("read gobby-code Cargo.toml");
        assert!(cargo.contains("name = \"gobby-code\""));
        assert!(cargo.contains("name = \"gcode\""));
        assert!(cargo.contains("path = \"src/main.rs\""));
        assert!(cargo.contains("falkordb = \"0.2\""));
        assert!(cargo.contains("urlencoding = \"2\""));
        assert!(cargo.contains("base64"));
        assert!(cargo.contains("reqwest"));

        let lock = std::fs::read_to_string(manifest_dir.join("../../Cargo.lock"))
            .expect("read workspace Cargo.lock");
        assert!(lock.contains("name = \"falkordb\""));
        assert!(lock.contains("name = \"urlencoding\""));
        assert!(!lock.contains("name = \"neo4j\""));
        assert!(!lock.contains("name = \"neo4rs\""));
    }

    #[test]
    fn phase7_additional_query_fragments_visible() {
        let source = include_str!("falkor.rs");
        for fragment in [
            "depth.clamp(1, 5)",
            "limit.clamp(1, MAX_GRAPH_LIMIT)",
            "offset.min(MAX_GRAPH_LIMIT)",
            "src.id IN [{ids}]",
            "LIMIT {limit}",
            "fn blast_radius_query(depth: usize, limit: usize)",
        ] {
            assert!(source.contains(fragment), "missing {fragment}");
        }
    }
}
