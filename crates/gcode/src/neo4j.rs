//! Neo4j HTTP API client for graph queries and writes.
//!
//! Sends Cypher queries via POST /db/{database}/query/v2 with Basic Auth.
//! All graph functions degrade gracefully — returning empty results on connection failure.
//!
//! Source: src/gobby/memory/neo4j_client.py, src/gobby/code_index/graph.py

use std::collections::HashMap;

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use serde_json::Value;

use crate::config::{Context, Neo4jConfig};
use crate::models::{CallRelation, GraphResult, ImportRelation, Symbol};

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
            .or_else(|| row.get("source_id"))
            .or_else(|| row.get("symbol_id"))
            .or_else(|| row.get("id"))
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        name: row
            .get("caller_name")
            .or_else(|| row.get("source_name"))
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
pub fn count_callers(ctx: &Context, symbol_name: &str) -> anyhow::Result<usize> {
    with_neo4j(ctx, 0, |client| {
        let rows = client.query(
            "MATCH (caller:CodeSymbol)-[:CALLS]->(callee:CodeSymbol {name: $name, project: $project}) \
             RETURN count(caller) AS cnt",
            Some(serde_json::json!({
                "name": symbol_name,
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
pub fn count_usages(ctx: &Context, symbol_name: &str) -> anyhow::Result<usize> {
    with_neo4j(ctx, 0, |client| {
        let rows = client.query(
            "MATCH (n)-[r]->(target:CodeSymbol {name: $name, project: $project}) \
             WHERE type(r) IN ['CALLS', 'IMPORTS'] \
             RETURN count(n) AS cnt",
            Some(serde_json::json!({
                "name": symbol_name,
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

/// Find symbols that call the given symbol name.
pub fn find_callers(
    ctx: &Context,
    symbol_name: &str,
    offset: usize,
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    with_neo4j(ctx, vec![], |client| {
        let rows = client.query(
            "MATCH (caller:CodeSymbol)-[r:CALLS]->(callee:CodeSymbol {name: $name, project: $project}) \
             RETURN caller.id AS caller_id, caller.name AS caller_name, \
                    r.file AS file, r.line AS line \
             SKIP $offset LIMIT $limit",
            Some(serde_json::json!({
                "name": symbol_name,
                "project": ctx.project_id,
                "offset": offset,
                "limit": limit,
            })),
        )?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

/// Find all usages of a symbol (callers + imports).
pub fn find_usages(
    ctx: &Context,
    symbol_name: &str,
    offset: usize,
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    with_neo4j(ctx, vec![], |client| {
        let rows = client.query(
            "MATCH (n)-[r]->(target:CodeSymbol {name: $name, project: $project}) \
             WHERE type(r) IN ['CALLS', 'IMPORTS'] \
             RETURN n.id AS source_id, n.name AS source_name, \
                    type(r) AS rel_type, r.file AS file, r.line AS line \
             SKIP $offset LIMIT $limit",
            Some(serde_json::json!({
                "name": symbol_name,
                "project": ctx.project_id,
                "offset": offset,
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

/// Find transitive blast radius of changing a symbol.
pub fn blast_radius(ctx: &Context, target: &str, depth: usize) -> anyhow::Result<Vec<GraphResult>> {
    let depth = depth.clamp(1, 5);
    with_neo4j(ctx, vec![], |client| {
        // Neo4j doesn't support parameterized path length, so we interpolate depth
        // (it's clamped to 1-5, safe for interpolation)
        let cypher = format!(
            "MATCH path = (affected:CodeSymbol)-[:CALLS*1..{depth}]->(\
                target:CodeSymbol {{name: $name, project: $project}}) \
             WITH affected, min(length(path)) AS distance \
             OPTIONAL MATCH (file:CodeFile)-[:DEFINES]->(affected) \
             RETURN DISTINCT affected.id AS symbol_id, \
                    affected.name AS symbol_name, \
                    affected.kind AS kind, file.path AS file_path, \
                    distance \
             ORDER BY distance ASC, affected.name ASC \
             LIMIT $limit"
        );
        let rows = client.query(
            &cypher,
            Some(serde_json::json!({
                "name": target,
                "project": ctx.project_id,
                "limit": 100,
            })),
        )?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

// ── Graph write functions (for indexing) ──────────────────────────────

/// Write DEFINES edges: file → symbol.
pub fn write_defines(client: &Neo4jClient, project_id: &str, file_path: &str, symbols: &[Symbol]) {
    for sym in symbols {
        let _ = client.query(
            "MERGE (f:CodeFile {path: $file, project: $project}) \
             MERGE (s:CodeSymbol {id: $symbol_id, project: $project}) \
             SET s.name = $name, s.kind = $kind \
             MERGE (f)-[:DEFINES]->(s)",
            Some(serde_json::json!({
                "file": file_path,
                "project": project_id,
                "symbol_id": sym.id,
                "name": sym.name,
                "kind": sym.kind,
            })),
        );
    }
}

/// Write CALLS edges: caller → callee.
pub fn write_calls(client: &Neo4jClient, project_id: &str, calls: &[CallRelation]) {
    for call in calls {
        let _ = client.query(
            "MERGE (caller:CodeSymbol {id: $caller_id, project: $project}) \
             MERGE (callee:CodeSymbol {name: $callee_name, project: $project}) \
             MERGE (caller)-[:CALLS {file: $file, line: $line}]->(callee)",
            Some(serde_json::json!({
                "caller_id": call.caller_id,
                "callee_name": call.callee_name,
                "file": call.file_path,
                "line": call.line,
                "project": project_id,
            })),
        );
    }
}

/// Write IMPORTS edges: file → module.
pub fn write_imports(client: &Neo4jClient, project_id: &str, imports: &[ImportRelation]) {
    for imp in imports {
        let _ = client.query(
            "MERGE (f:CodeFile {path: $source, project: $project}) \
             MERGE (m:CodeModule {name: $target, project: $project}) \
             MERGE (f)-[:IMPORTS]->(m)",
            Some(serde_json::json!({
                "source": imp.file_path,
                "target": imp.module_name,
                "project": project_id,
            })),
        );
    }
}

/// Delete graph data for a file before re-indexing.
///
/// Preserves incoming CALLS edges to symbol nodes (from other files).
/// Symbol nodes become orphaned but are re-merged by write_defines.
pub fn delete_file_graph(client: &Neo4jClient, project_id: &str, file_path: &str) {
    // 1. Delete outgoing relationships from symbols defined in this file
    // 2. Detach-delete the CodeFile node (removes DEFINES + IMPORTS edges)
    // Symbol nodes remain with incoming CALLS edges intact.
    let _ = client.query(
        "MATCH (f:CodeFile {path: $file_path, project: $project}) \
         OPTIONAL MATCH (f)-[:DEFINES]->(s:CodeSymbol) \
         OPTIONAL MATCH (s)-[r_out]->() \
         DELETE r_out \
         WITH f \
         DETACH DELETE f",
        Some(serde_json::json!({
            "file_path": file_path,
            "project": project_id,
        })),
    );
    // Clean up orphaned modules
    let _ = client.query(
        "MATCH (m:CodeModule {project: $project}) \
         WHERE NOT (m)<-[:IMPORTS]-() \
         DETACH DELETE m",
        Some(serde_json::json!({
            "project": project_id,
        })),
    );
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
}
