//! Neo4j HTTP API client for graph queries and writes.
//!
//! Sends Cypher queries via POST /db/{database}/query/v2 with Basic Auth.
//!
//! Read helpers (wrapped by `with_neo4j`) degrade gracefully — returning
//! empty results on connection failure. Write functions (`write_defines`,
//! `write_calls`, `write_imports`, `delete_file_graph`) propagate errors
//! to callers via `Result<()>` so failures can be tracked.

use std::collections::HashMap;

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use serde_json::Value;

use crate::config::{Context, Neo4jConfig};
use crate::models::GraphResult;

const CALL_TARGET_PREDICATE: &str =
    "target:CodeSymbol OR target:UnresolvedCallee OR target:ExternalSymbol";

/// Row from a Neo4j v2 query response.
pub type Row = HashMap<String, Value>;

/// Blocking HTTP client for the Neo4j Query API v2.
pub struct Neo4jClient {
    client: reqwest::blocking::Client,
    url: String,
    database: String,
    auth_header: Option<String>,
}

impl Neo4jClient {
    pub fn from_config(config: &Neo4jConfig) -> Self {
        let auth_header = config
            .auth
            .as_ref()
            .map(|a| format!("Basic {}", STANDARD.encode(a.as_bytes())));
        Self {
            client: reqwest::blocking::Client::builder()
                .timeout(std::time::Duration::from_secs(15))
                .build()
                .expect("failed to build HTTP client"),
            url: config.url.trim_end_matches('/').to_string(),
            database: config.database.clone(),
            auth_header,
        }
    }

    /// Execute a Cypher query and return parsed rows.
    pub fn query(&self, cypher: &str, params: Option<Value>) -> anyhow::Result<Vec<Row>> {
        let path = format!("{}/db/{}/query/v2", self.url, self.database);
        let mut body = serde_json::json!({"statement": cypher});
        if let Some(p) = params {
            body["parameters"] = p;
        }

        let mut req = self
            .client
            .post(&path)
            .header("Content-Type", "application/json")
            .header("Accept", "application/json")
            .json(&body);

        if let Some(auth) = &self.auth_header {
            req = req.header("Authorization", auth);
        }

        let response = req.send()?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().unwrap_or_default();
            anyhow::bail!("Neo4j query error: HTTP {status}: {body}");
        }

        let data: Value = response.json()?;
        Ok(parse_v2_response(&data))
    }
}

/// Parse Neo4j HTTP API v2 response into flat row dicts.
/// Format: {"data": {"fields": [...], "values": [[...], ...]}}
fn parse_v2_response(data: &Value) -> Vec<Row> {
    let result_data = data.get("data").unwrap_or(&Value::Null);
    let fields: Vec<String> = result_data
        .get("fields")
        .and_then(|f| f.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default();
    let values = result_data
        .get("values")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();

    values
        .into_iter()
        .filter_map(|row_val| {
            let row_arr = row_val.as_array()?;
            let mut row = HashMap::new();
            for (i, field) in fields.iter().enumerate() {
                let val = row_arr.get(i).cloned().unwrap_or(Value::Null);
                row.insert(field.clone(), val);
            }
            Some(row)
        })
        .collect()
}

// ── Helper: run graph query with graceful degradation ────────────────

fn with_neo4j<T>(
    ctx: &Context,
    default: T,
    f: impl FnOnce(&Neo4jClient) -> anyhow::Result<T>,
) -> anyhow::Result<T> {
    match &ctx.neo4j {
        Some(config) => {
            let client = Neo4jClient::from_config(config);
            match f(&client) {
                Ok(v) => Ok(v),
                Err(e) => {
                    if !ctx.quiet {
                        eprintln!("Warning: Neo4j query failed: {e}");
                    }
                    Ok(default)
                }
            }
        }
        None => Ok(default),
    }
}

fn row_to_graph_result(row: &Row) -> GraphResult {
    GraphResult {
        id: row
            .get("caller_id")
            .or_else(|| row.get("callee_id"))
            .or_else(|| row.get("source_id"))
            .or_else(|| row.get("node_id"))
            .or_else(|| row.get("symbol_id"))
            .or_else(|| row.get("id"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        name: row
            .get("caller_name")
            .or_else(|| row.get("callee_name"))
            .or_else(|| row.get("source_name"))
            .or_else(|| row.get("node_name"))
            .or_else(|| row.get("symbol_name"))
            .or_else(|| row.get("name"))
            .or_else(|| row.get("module_name"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        file_path: row
            .get("file")
            .or_else(|| row.get("file_path"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        line: row.get("line").and_then(|v| v.as_u64()).unwrap_or(0) as usize,
        relation: row
            .get("relation")
            .or_else(|| row.get("rel_type"))
            .and_then(|v| v.as_str())
            .map(String::from),
        distance: row
            .get("distance")
            .and_then(|v| v.as_u64())
            .map(|d| d as usize),
    }
}

// ── Graph query functions (read) ─────────────────────────────────────

/// Count callers of a symbol (server-side COUNT for accurate pagination total).
pub fn count_callers(ctx: &Context, symbol_id: &str) -> anyhow::Result<usize> {
    with_neo4j(ctx, 0, |client| {
        let rows = client.query(
            &format!(
                "MATCH (caller:CodeSymbol {{project: $project}})-[:CALLS]->(target {{id: $id, project: $project}}) \
                 WHERE {CALL_TARGET_PREDICATE} \
             RETURN count(caller) AS cnt",
            ),
            Some(serde_json::json!({
                "id": symbol_id,
                "project": ctx.project_id,
            })),
        )?;
        let count = rows
            .first()
            .and_then(|r| r.get("cnt"))
            .and_then(|v| v.as_u64())
            .unwrap_or(0) as usize;
        Ok(count)
    })
}

/// Count usages of a symbol (server-side COUNT for accurate pagination total).
pub fn count_usages(ctx: &Context, symbol_id: &str) -> anyhow::Result<usize> {
    with_neo4j(ctx, 0, |client| {
        let rows = client.query(
            &format!(
                "MATCH (source:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{id: $id, project: $project}}) \
                 WHERE {CALL_TARGET_PREDICATE} \
                 RETURN count(source) AS cnt",
            ),
            Some(serde_json::json!({
                "id": symbol_id,
                "project": ctx.project_id,
            })),
        )?;
        let count = rows
            .first()
            .and_then(|r| r.get("cnt"))
            .and_then(|v| v.as_u64())
            .unwrap_or(0) as usize;
        Ok(count)
    })
}

/// Find symbols that call the given symbol id.
pub fn find_callers(
    ctx: &Context,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    with_neo4j(ctx, vec![], |client| {
        let rows = client.query(
            &format!(
                "MATCH (caller:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{id: $id, project: $project}}) \
                 WHERE {CALL_TARGET_PREDICATE} \
                 RETURN caller.id AS caller_id, caller.name AS caller_name, \
                        r.file AS file, r.line AS line \
                 SKIP $offset LIMIT $limit",
            ),
            Some(serde_json::json!({
                "id": symbol_id,
                "project": ctx.project_id,
                "offset": offset,
                "limit": limit,
            })),
        )?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

/// Find incoming CALLS usages for a canonical, unresolved, or external target.
pub fn find_usages(
    ctx: &Context,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    with_neo4j(ctx, vec![], |client| {
        let rows = client.query(
            &format!(
                "MATCH (source:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{id: $id, project: $project}}) \
                 WHERE {CALL_TARGET_PREDICATE} \
                 RETURN source.id AS source_id, source.name AS source_name, \
                        'CALLS' AS rel_type, r.file AS file, r.line AS line \
                 SKIP $offset LIMIT $limit",
            ),
            Some(serde_json::json!({
                "id": symbol_id,
                "project": ctx.project_id,
                "offset": offset,
                "limit": limit,
            })),
        )?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

/// Find symbols that call any of the given target ids (batch).
/// Used by graph expansion to find callers of top search results.
pub fn find_callers_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    if symbol_ids.is_empty() {
        return Ok(vec![]);
    }
    with_neo4j(ctx, vec![], |client| {
        let rows = client.query(
            &format!(
                "MATCH (caller:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{project: $project}}) \
                 WHERE ({CALL_TARGET_PREDICATE}) AND target.id IN $ids \
                 RETURN caller.id AS caller_id, caller.name AS caller_name, \
                        r.file AS file, r.line AS line \
                 LIMIT $limit",
            ),
            Some(serde_json::json!({
                "ids": symbol_ids,
                "project": ctx.project_id,
                "limit": limit,
            })),
        )?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

/// Find call targets reached by any of the given source ids (batch).
/// Used by graph expansion to find callees of top search results.
pub fn find_callees_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    if symbol_ids.is_empty() {
        return Ok(vec![]);
    }
    with_neo4j(ctx, vec![], |client| {
        let rows = client.query(
            &format!(
                "MATCH (src:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{project: $project}}) \
                 WHERE src.id IN $ids AND ({CALL_TARGET_PREDICATE}) \
                 RETURN target.id AS callee_id, target.name AS callee_name, \
                        r.file AS file, r.line AS line \
                 LIMIT $limit",
            ),
            Some(serde_json::json!({
                "ids": symbol_ids,
                "project": ctx.project_id,
                "limit": limit,
            })),
        )?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

/// Get import graph for a file.
pub fn get_imports(ctx: &Context, file_path: &str) -> anyhow::Result<Vec<GraphResult>> {
    with_neo4j(ctx, vec![], |client| {
        let rows = client.query(
            "MATCH (f:CodeFile {path: $path, project: $project})-[:IMPORTS]->(m:CodeModule) \
             RETURN m.name AS module_name",
            Some(serde_json::json!({
                "path": file_path,
                "project": ctx.project_id,
            })),
        )?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

fn blast_radius_query(depth: usize) -> String {
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
         LIMIT $limit"
    )
}

/// Find transitive blast radius of changing a symbol.
pub fn blast_radius(
    ctx: &Context,
    symbol_id: &str,
    depth: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    let depth = depth.clamp(1, 5);
    with_neo4j(ctx, vec![], |client| {
        // Neo4j doesn't support parameterized path length, so we interpolate depth
        // (it's clamped to 1-5, safe for interpolation)
        let rows = client.query(
            &blast_radius_query(depth),
            Some(serde_json::json!({
                "id": symbol_id,
                "project": ctx.project_id,
                "limit": 100,
            })),
        )?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_v2_response_basic() {
        let data = serde_json::json!({
            "data": {
                "fields": ["name", "age"],
                "values": [
                    ["Alice", 30],
                    ["Bob", 25]
                ]
            }
        });
        let rows = parse_v2_response(&data);
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["name"], "Alice");
        assert_eq!(rows[0]["age"], 30);
        assert_eq!(rows[1]["name"], "Bob");
    }

    #[test]
    fn test_parse_v2_response_empty() {
        let data = serde_json::json!({"data": {"fields": [], "values": []}});
        let rows = parse_v2_response(&data);
        assert!(rows.is_empty());
    }

    #[test]
    fn test_parse_v2_response_null_values() {
        let data = serde_json::json!({
            "data": {
                "fields": ["id", "name"],
                "values": [
                    ["abc", null]
                ]
            }
        });
        let rows = parse_v2_response(&data);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["id"], "abc");
        assert!(rows[0]["name"].is_null());
    }

    #[test]
    fn test_parse_v2_response_mismatched_lengths() {
        let data = serde_json::json!({
            "data": {
                "fields": ["a", "b", "c"],
                "values": [
                    ["x"]
                ]
            }
        });
        let rows = parse_v2_response(&data);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["a"], "x");
        assert!(rows[0]["b"].is_null());
        assert!(rows[0]["c"].is_null());
    }

    #[test]
    fn test_parse_v2_response_missing_data() {
        let data = serde_json::json!({});
        let rows = parse_v2_response(&data);
        assert!(rows.is_empty());
    }

    #[test]
    fn test_row_to_graph_result_prefers_blast_radius_node_fields() {
        let row = HashMap::from([
            ("node_id".to_string(), serde_json::json!("sym-1")),
            ("node_name".to_string(), serde_json::json!("foo")),
            ("file_path".to_string(), serde_json::json!("src/main.py")),
            ("line".to_string(), serde_json::json!(42)),
            ("rel_type".to_string(), serde_json::json!("call")),
            ("distance".to_string(), serde_json::json!(2)),
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
    fn test_blast_radius_query_targets_stable_ids_and_all_target_labels() {
        let query = blast_radius_query(3);

        assert!(query.contains("target {id: $id, project: $project}"));
        assert!(query.contains(CALL_TARGET_PREDICATE));
        assert!(query.contains("[:CALLS*1..3]"));
    }
}
