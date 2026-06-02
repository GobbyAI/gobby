use std::collections::{HashMap, HashSet};

use crate::config::Context;
use crate::graph::typed_query;
use crate::models::GraphResult;
use gobby_core::falkor::Row;

use super::connection::with_required_core_graph;
use super::payload::{
    GraphBlastRadiusTarget, GraphLink, GraphNode, GraphPayload, add_link_from_row,
    add_node_from_row, add_prefixed_node_from_row, row_string_owned, row_to_projection_metadata,
    row_usize,
};

const CALL_TARGET_PREDICATE: &str =
    "target:CodeSymbol OR target:UnresolvedCallee OR target:ExternalSymbol";
const NEIGHBOR_PREDICATE: &str =
    "neighbor:CodeSymbol OR neighbor:UnresolvedCallee OR neighbor:ExternalSymbol";
const TARGET_TYPE_CASE: &str = "CASE \
     WHEN target:CodeSymbol THEN coalesce(target.kind, 'function') \
     WHEN target:ExternalSymbol THEN 'external' \
     ELSE 'unresolved' \
     END";
const NEIGHBOR_TYPE_CASE: &str = "CASE \
     WHEN neighbor:CodeSymbol THEN coalesce(neighbor.kind, 'function') \
     WHEN neighbor:ExternalSymbol THEN 'external' \
     ELSE 'unresolved' \
     END";
const NODE_TYPE_CASE: &str = "CASE \
     WHEN n:CodeFile THEN 'file' \
     WHEN n:CodeModule THEN 'module' \
     WHEN n:CodeSymbol THEN coalesce(n.kind, 'function') \
     WHEN n:ExternalSymbol THEN 'external' \
     ELSE 'unresolved' \
     END";
const LINK_METADATA_RETURN: &str = "r.provenance AS provenance, \
     r.confidence AS confidence, \
     r.source_system AS source_system, \
     r.source_file_path AS metadata_source_file_path, \
     r.source_line AS source_line, \
     r.source_symbol_id AS source_symbol_id, \
     r.matching_method AS matching_method";
const MAX_GRAPH_LIMIT: usize = 100;

pub(crate) fn row_to_graph_result(row: &Row) -> GraphResult {
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
        line: row
            .get("line")
            .and_then(|v| v.as_u64())
            .and_then(|value| usize::try_from(value).ok())
            .unwrap_or(0),
        relation: row
            .get("relation")
            .or_else(|| row.get("rel_type"))
            .and_then(|v| v.as_str())
            .map(String::from),
        distance: row
            .get("distance")
            .and_then(|v| v.as_u64())
            .and_then(|d| usize::try_from(d).ok()),
        metadata: row_to_projection_metadata(row),
    }
}
fn clamp_limit(limit: usize) -> usize {
    typed_query::clamp_limit(limit, MAX_GRAPH_LIMIT)
}

fn clamp_offset(offset: usize) -> usize {
    typed_query::clamp_offset(offset, MAX_GRAPH_LIMIT)
}

pub(crate) fn count_callers_query(
    project_id: &str,
    symbol_id: &str,
) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (caller:CodeSymbol {{project: $project}})-[:CALLS]->(target {{id: $id, project: $project}}) \
             WHERE {CALL_TARGET_PREDICATE} \
             RETURN count(DISTINCT caller) AS cnt"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub(crate) fn count_usages_query(
    project_id: &str,
    symbol_id: &str,
) -> (String, HashMap<String, String>) {
    // Keep this separate from count_callers_query even though both currently
    // count CALLS edges; callers is the direct-caller API, usages is the wider
    // command surface that can grow to imports/references.
    (
        format!(
            "MATCH (source:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{id: $id, project: $project}}) \
             WHERE {CALL_TARGET_PREDICATE} \
             RETURN count(source) AS cnt"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub(crate) fn find_callers_query(
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
             RETURN DISTINCT caller.id AS caller_id, caller.name AS caller_name, \
                    caller.file_path AS file, caller.line_start AS line \
             ORDER BY caller.id \
             SKIP {offset} LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub(crate) fn find_usages_query(
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
             ORDER BY source.id, r.line, r.file \
             SKIP {offset} LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

fn find_caller_ids_query(
    project_id: &str,
    symbol_id: &str,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    (
        format!(
            "MATCH (caller:CodeSymbol {{project: $project}})-[:CALLS]->(target {{id: $id, project: $project}}) \
             WHERE {CALL_TARGET_PREDICATE} \
             RETURN DISTINCT caller.id AS id \
             ORDER BY caller.id \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

fn find_usage_ids_query(
    project_id: &str,
    symbol_id: &str,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    (
        format!(
            "MATCH (source:CodeSymbol {{project: $project}})-[:CALLS]->(target {{id: $id, project: $project}}) \
             WHERE {CALL_TARGET_PREDICATE} \
             RETURN DISTINCT source.id AS id \
             ORDER BY source.id \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub(crate) fn find_callers_batch_query(
    project_id: &str,
    symbol_ids: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let ids = typed_query::id_list_literal(symbol_ids);
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

fn find_caller_ids_batch_query(
    project_id: &str,
    symbol_ids: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let ids = typed_query::id_list_literal(symbol_ids);
    (
        format!(
            "MATCH (caller:CodeSymbol {{project: $project}})-[:CALLS]->(target {{project: $project}}) \
             WHERE ({CALL_TARGET_PREDICATE}) AND target.id IN [{ids}] \
             RETURN DISTINCT caller.id AS id \
             ORDER BY caller.id \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub(crate) fn find_callees_batch_query(
    project_id: &str,
    symbol_ids: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let ids = typed_query::id_list_literal(symbol_ids);
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

fn find_callee_ids_batch_query(
    project_id: &str,
    symbol_ids: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let ids = typed_query::id_list_literal(symbol_ids);
    (
        format!(
            "MATCH (src:CodeSymbol {{project: $project}})-[:CALLS]->(target {{project: $project}}) \
             WHERE src.id IN [{ids}] AND ({CALL_TARGET_PREDICATE}) \
             RETURN DISTINCT target.id AS id \
             ORDER BY target.id \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub(crate) fn get_imports_query(
    project_id: &str,
    file_path: &str,
) -> (String, HashMap<String, String>) {
    (
        "MATCH (f:CodeFile {path: $path, project: $project})-[:IMPORTS]->(m:CodeModule) \
         RETURN m.name AS id, m.name AS module_name"
            .to_string(),
        typed_query::string_params(&[("project", project_id), ("path", file_path)]),
    )
}

pub(crate) fn blast_radius_query(depth: usize, limit: usize) -> String {
    let depth = depth.clamp(1, 5);
    let limit = clamp_limit(limit);
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

fn project_overview_files_query(
    project_id: &str,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    (
        format!(
            "MATCH (f:CodeFile {{project: $project}}) \
             OPTIONAL MATCH (f)-[:DEFINES]->(s:CodeSymbol) \
             WITH f, count(DISTINCT s) AS sym_count \
             OPTIONAL MATCH (f)-[:IMPORTS]->(m:CodeModule) \
             WITH f, sym_count, count(m) AS imp_count \
             RETURN f.path AS id, f.path AS name, 'file' AS type, \
                    f.path AS file_path, sym_count AS symbol_count \
             ORDER BY imp_count DESC, sym_count DESC, f.path \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

fn project_overview_imports_query(
    project_id: &str,
    file_paths: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let file_paths = typed_query::id_list_literal(file_paths);
    (
        format!(
            "MATCH (f:CodeFile {{project: $project}})-[r:IMPORTS]->(m:CodeModule {{project: $project}}) \
             WHERE f.path IN [{file_paths}] \
             RETURN f.path AS source, m.name AS target, 'IMPORTS' AS type, {LINK_METADATA_RETURN} \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

fn project_overview_defines_query(
    project_id: &str,
    file_paths: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let file_paths = typed_query::id_list_literal(file_paths);
    (
        format!(
            "MATCH (f:CodeFile {{project: $project}})-[r:DEFINES]->(s:CodeSymbol {{project: $project}}) \
             WHERE f.path IN [{file_paths}] \
             RETURN f.path AS source, s.id AS target, 'DEFINES' AS type, \
                    s.name AS symbol_name, s.kind AS symbol_kind, \
                    s.file_path AS symbol_file_path, s.line_start AS line_start, \
                    {LINK_METADATA_RETURN} \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

fn project_overview_calls_query(
    project_id: &str,
    file_paths: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let file_paths = typed_query::id_list_literal(file_paths);
    (
        format!(
            "MATCH (f:CodeFile {{project: $project}})-[:DEFINES]->(s:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{project: $project}}) \
             WHERE f.path IN [{file_paths}] AND ({CALL_TARGET_PREDICATE}) \
             RETURN s.id AS source, target.id AS target, 'CALLS' AS type, \
                    target.name AS target_name, {TARGET_TYPE_CASE} AS target_type, \
                    target.kind AS target_kind, target.file_path AS target_file_path, \
                    target.line_start AS target_line_start, r.line AS line, \
                    {LINK_METADATA_RETURN} \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

fn file_symbols_query(project_id: &str, file_path: &str) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (:CodeFile {{path: $path, project: $project}})-[r:DEFINES]->(s:CodeSymbol {{project: $project}}) \
             RETURN s.id AS id, s.name AS name, coalesce(s.kind, 'function') AS type, \
                    s.kind AS kind, s.file_path AS file_path, \
                    s.line_start AS line_start, s.signature AS signature, \
                    {LINK_METADATA_RETURN}"
        ),
        typed_query::string_params(&[("project", project_id), ("path", file_path)]),
    )
}

pub(super) fn file_calls_query(
    project_id: &str,
    file_path: &str,
) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (source:CodeSymbol {{project: $project}})-[r:CALLS]->(target {{project: $project}}) \
             WHERE ({CALL_TARGET_PREDICATE}) \
               AND (source.file_path = $path OR (target:CodeSymbol AND target.file_path = $path)) \
             RETURN source.id AS source_id, source.name AS source_name, \
                    coalesce(source.kind, 'function') AS source_type, \
                    source.kind AS source_kind, source.file_path AS source_file_path, \
                    source.line_start AS source_line_start, source.signature AS source_signature, \
                    target.id AS target_id, target.name AS target_name, \
                    {TARGET_TYPE_CASE} AS target_type, target.kind AS target_kind, \
                    target.file_path AS target_file_path, \
                    target.line_start AS target_line_start, target.signature AS target_signature, \
                    source.id AS source, target.id AS target, 'CALLS' AS type, r.line AS line, \
                    {LINK_METADATA_RETURN}"
        ),
        typed_query::string_params(&[("project", project_id), ("path", file_path)]),
    )
}

fn symbol_neighbors_query(
    project_id: &str,
    symbol_id: &str,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    (
        format!(
            "MATCH (center {{id: $id, project: $project}}) \
             WHERE center:CodeSymbol OR center:UnresolvedCallee OR center:ExternalSymbol \
             MATCH (center)-[r:CALLS]-(neighbor {{project: $project}}) \
             WHERE {NEIGHBOR_PREDICATE} \
             RETURN neighbor.id AS id, neighbor.name AS name, {NEIGHBOR_TYPE_CASE} AS type, \
                    neighbor.kind AS kind, neighbor.file_path AS file_path, \
                    neighbor.line_start AS line_start, neighbor.signature AS signature, \
                    CASE WHEN startNode(r) = center THEN 'outgoing' ELSE 'incoming' END AS direction, \
                    r.line AS line, {LINK_METADATA_RETURN} \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

fn blast_radius_center_query(
    project_id: &str,
    symbol_id: &str,
) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (n {{id: $id, project: $project}}) \
             WHERE n:CodeSymbol OR n:UnresolvedCallee OR n:ExternalSymbol \
             RETURN n.id AS id, n.name AS name, {NODE_TYPE_CASE} AS type, \
                    n.kind AS kind, n.file_path AS file_path \
             LIMIT 1"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

fn blast_radius_file_call_query(
    project_id: &str,
    file_path: &str,
    depth: usize,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let depth = depth.clamp(1, 5);
    let limit = clamp_limit(limit);
    (
        format!(
            "MATCH (tf:CodeFile {{path: $path, project: $project}})-[:DEFINES]->(target_sym:CodeSymbol {{project: $project}}) \
             MATCH path = (affected:CodeSymbol {{project: $project}})-[:CALLS*1..{depth}]->(target_sym) \
             WITH affected, min(length(path)) AS distance \
             OPTIONAL MATCH (file:CodeFile {{project: $project}})-[:DEFINES]->(affected) \
             RETURN DISTINCT affected.id AS node_id, \
                    affected.name AS node_name, \
                    affected.kind AS kind, file.path AS file_path, \
                    affected.line_start AS line, distance, 'call' AS rel_type, \
                    coalesce(affected.kind, 'function') AS node_type \
             ORDER BY distance ASC, affected.name ASC \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("path", file_path)]),
    )
}

pub(super) fn blast_radius_file_import_query(
    project_id: &str,
    file_path: &str,
    depth: usize,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let depth = depth.clamp(1, 5);
    let limit = clamp_limit(limit);
    (
        format!(
            "MATCH (tf:CodeFile {{path: $path, project: $project}})-[:IMPORTS]->(m:CodeModule {{project: $project}}) \
             MATCH path = (importer:CodeFile {{project: $project}})-[:IMPORTS*1..{depth}]-(m) \
             WHERE importer.path <> $path \
             WITH importer, min(length(path)) AS distance \
             RETURN DISTINCT importer.path AS node_id, \
                    importer.path AS node_name, NULL AS kind, importer.path AS file_path, \
                    NULL AS line, distance, 'import' AS rel_type, 'file' AS node_type \
             ORDER BY distance ASC \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("path", file_path)]),
    )
}

pub(super) fn dedupe_limited_blast_rows(mut rows: Vec<Row>, limit: usize) -> Vec<Row> {
    rows.sort_by(|left, right| {
        row_usize(left, &["distance"])
            .unwrap_or(usize::MAX)
            .cmp(&row_usize(right, &["distance"]).unwrap_or(usize::MAX))
            .then_with(|| {
                row_string_owned(left, &["node_name"])
                    .unwrap_or_default()
                    .cmp(&row_string_owned(right, &["node_name"]).unwrap_or_default())
            })
            .then_with(|| {
                row_string_owned(left, &["node_id"])
                    .unwrap_or_default()
                    .cmp(&row_string_owned(right, &["node_id"]).unwrap_or_default())
            })
    });

    let mut seen = HashSet::new();
    rows.retain(|row| {
        let Some(node_id) = row_string_owned(row, &["node_id"]) else {
            return false;
        };
        seen.insert(node_id)
    });
    rows.truncate(clamp_limit(limit));
    rows
}

fn count_from_rows(rows: &[Row]) -> usize {
    rows.first()
        .and_then(|r| r.get("cnt"))
        .and_then(|v| {
            v.as_u64()
                .or_else(|| v.as_i64().and_then(|value| value.try_into().ok()))
        })
        .and_then(|value| usize::try_from(value).ok())
        .unwrap_or(0)
}

pub fn project_overview_graph(ctx: &Context, limit: usize) -> anyhow::Result<GraphPayload> {
    with_required_core_graph(ctx, |client| {
        let limit = clamp_limit(limit);
        let link_limit = clamp_limit(limit.saturating_mul(4));
        let max_nodes = limit.saturating_mul(8);

        let (query, params) = project_overview_files_query(&ctx.project_id, limit);
        let file_rows = client.query(&query, Some(params))?;
        let mut payload = GraphPayload::default();
        for row in &file_rows {
            add_node_from_row(&mut payload, row, "file");
        }

        let file_paths = payload
            .nodes
            .iter()
            .filter(|node| node.node_type == "file")
            .map(|node| node.id.clone())
            .collect::<Vec<_>>();
        if file_paths.is_empty() {
            return Ok(payload);
        }

        let (query, params) =
            project_overview_imports_query(&ctx.project_id, &file_paths, link_limit);
        for row in client.query(&query, Some(params))? {
            add_link_from_row(&mut payload, &row);
            if let Some(module_id) = row_string_owned(&row, &["target"]) {
                payload.push_node(GraphNode::new(module_id.clone(), module_id, "module"));
            }
            if payload.nodes.len() >= max_nodes {
                break;
            }
        }

        let (query, params) =
            project_overview_defines_query(&ctx.project_id, &file_paths, link_limit);
        for row in client.query(&query, Some(params))? {
            add_link_from_row(&mut payload, &row);
            if let Some(symbol_id) = row_string_owned(&row, &["target"]) {
                let mut node = GraphNode::new(
                    symbol_id.clone(),
                    row_string_owned(&row, &["symbol_name"]).unwrap_or(symbol_id),
                    row_string_owned(&row, &["symbol_kind"])
                        .unwrap_or_else(|| "function".to_string()),
                );
                node.kind = row_string_owned(&row, &["symbol_kind"]);
                node.file_path = row_string_owned(&row, &["symbol_file_path", "source"]);
                node.line_start = row_usize(&row, &["line_start"]);
                payload.push_node(node);
            }
            if payload.nodes.len() >= max_nodes {
                break;
            }
        }

        let (query, params) =
            project_overview_calls_query(&ctx.project_id, &file_paths, link_limit);
        for row in client.query(&query, Some(params))? {
            add_link_from_row(&mut payload, &row);
            if let Some(target_id) = row_string_owned(&row, &["target"]) {
                let mut node = GraphNode::new(
                    target_id.clone(),
                    row_string_owned(&row, &["target_name"]).unwrap_or(target_id),
                    row_string_owned(&row, &["target_type"])
                        .unwrap_or_else(|| "unresolved".to_string()),
                );
                node.kind = row_string_owned(&row, &["target_kind"]);
                node.file_path = row_string_owned(&row, &["target_file_path"]);
                node.line_start = row_usize(&row, &["target_line_start"]);
                payload.push_node(node);
            }
            if payload.nodes.len() >= max_nodes {
                break;
            }
        }

        Ok(payload)
    })
}

pub fn file_graph(ctx: &Context, file_path: &str) -> anyhow::Result<GraphPayload> {
    with_required_core_graph(ctx, |client| {
        let mut payload = GraphPayload::default();
        let mut file_node = GraphNode::new(file_path, file_path, "file");
        file_node.file_path = Some(file_path.to_string());
        payload.push_node(file_node);

        let (query, params) = file_symbols_query(&ctx.project_id, file_path);
        for row in client.query(&query, Some(params))? {
            add_node_from_row(&mut payload, &row, "function");
            if let Some(symbol_id) = row_string_owned(&row, &["id"]) {
                let mut link = GraphLink::new(file_path, symbol_id, "DEFINES");
                link.metadata = row_to_projection_metadata(&row);
                payload.links.push(link);
            }
        }

        let (query, params) = file_calls_query(&ctx.project_id, file_path);
        for row in client.query(&query, Some(params))? {
            add_prefixed_node_from_row(&mut payload, &row, "source", "function");
            add_prefixed_node_from_row(&mut payload, &row, "target", "unresolved");
            add_link_from_row(&mut payload, &row);
        }

        Ok(payload)
    })
}

pub fn symbol_neighbors(
    ctx: &Context,
    symbol_id: &str,
    limit: usize,
) -> anyhow::Result<GraphPayload> {
    with_required_core_graph(ctx, |client| {
        let mut payload = GraphPayload::with_center(symbol_id.to_string());
        let (query, params) = blast_radius_center_query(&ctx.project_id, symbol_id);
        let center_rows = client.query(&query, Some(params))?;
        let center_node = center_rows
            .first()
            .and_then(|row| GraphNode::from_row(row, "function"))
            .unwrap_or_else(|| GraphNode::new(symbol_id, symbol_id, "function"));
        payload.push_node(center_node);

        let (query, params) = symbol_neighbors_query(&ctx.project_id, symbol_id, limit);
        let rows = client.query(&query, Some(params))?;

        for row in rows {
            add_node_from_row(&mut payload, &row, "unresolved");
            let Some(neighbor_id) = row_string_owned(&row, &["id"]) else {
                continue;
            };
            let direction = row_string_owned(&row, &["direction"]).unwrap_or_default();
            let mut link = if direction == "outgoing" {
                GraphLink::new(symbol_id, neighbor_id, "CALLS")
            } else {
                GraphLink::new(neighbor_id, symbol_id, "CALLS")
            };
            link.line = row_usize(&row, &["line"]);
            link.metadata = row_to_projection_metadata(&row);
            payload.links.push(link);
        }

        Ok(payload)
    })
}

pub fn blast_radius_graph(
    ctx: &Context,
    target: GraphBlastRadiusTarget,
    depth: usize,
    limit: usize,
) -> anyhow::Result<GraphPayload> {
    with_required_core_graph(ctx, |client| {
        let (center_id, mut center_node, rows) = match target {
            GraphBlastRadiusTarget::SymbolId(symbol_id) => {
                let (query, params) = blast_radius_center_query(&ctx.project_id, &symbol_id);
                let center_rows = client.query(&query, Some(params))?;
                let center_node = center_rows
                    .first()
                    .and_then(|row| GraphNode::from_row(row, "function"))
                    .unwrap_or_else(|| GraphNode::new(&symbol_id, &symbol_id, "function"));

                let query = blast_radius_query(depth, limit);
                let params =
                    typed_query::string_params(&[("project", &ctx.project_id), ("id", &symbol_id)]);
                (symbol_id, center_node, client.query(&query, Some(params))?)
            }
            GraphBlastRadiusTarget::FilePath(file_path) => {
                let mut rows = vec![];
                let (query, params) =
                    blast_radius_file_call_query(&ctx.project_id, &file_path, depth, limit);
                rows.extend(client.query(&query, Some(params))?);
                let (query, params) =
                    blast_radius_file_import_query(&ctx.project_id, &file_path, depth, limit);
                rows.extend(client.query(&query, Some(params))?);
                let rows = dedupe_limited_blast_rows(rows, limit);
                let mut center_node = GraphNode::new(&file_path, &file_path, "file");
                center_node.file_path = Some(file_path.clone());
                (file_path.clone(), center_node, rows)
            }
        };

        center_node.blast_distance = Some(0);
        let mut payload = GraphPayload::with_center(center_id.clone());
        payload.push_node(center_node);

        for row in rows {
            let Some(node_id) = row_string_owned(&row, &["node_id"]) else {
                continue;
            };
            let mut node = GraphNode::new(
                node_id.clone(),
                row_string_owned(&row, &["node_name"]).unwrap_or_else(|| node_id.clone()),
                row_string_owned(&row, &["node_type"]).unwrap_or_else(|| "function".to_string()),
            );
            node.kind = row_string_owned(&row, &["kind"]);
            node.file_path = row_string_owned(&row, &["file_path"]);
            node.line_start = row_usize(&row, &["line"]);
            node.blast_distance = row_usize(&row, &["distance"]);
            payload.push_node(node);

            let relation =
                row_string_owned(&row, &["rel_type"]).unwrap_or_else(|| "call".to_string());
            let mut link = GraphLink::new(
                node_id,
                &center_id,
                if relation == "call" {
                    "CALLS"
                } else {
                    "IMPORTS"
                },
            );
            link.distance = row_usize(&row, &["distance"]);
            link.metadata = row_to_projection_metadata(&row);
            payload.links.push(link);
        }

        Ok(payload)
    })
}

pub fn count_callers(ctx: &Context, symbol_id: &str) -> anyhow::Result<usize> {
    with_required_core_graph(ctx, |client| {
        let (query, params) = count_callers_query(&ctx.project_id, symbol_id);
        let rows = client.query(&query, Some(params))?;
        Ok(count_from_rows(&rows))
    })
}

pub fn count_usages(ctx: &Context, symbol_id: &str) -> anyhow::Result<usize> {
    with_required_core_graph(ctx, |client| {
        let (query, params) = count_usages_query(&ctx.project_id, symbol_id);
        let rows = client.query(&query, Some(params))?;
        Ok(count_from_rows(&rows))
    })
}

pub fn find_callers(
    ctx: &Context,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    with_required_core_graph(ctx, |client| {
        let (query, params) = find_callers_query(&ctx.project_id, symbol_id, offset, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn find_usages(
    ctx: &Context,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    with_required_core_graph(ctx, |client| {
        let (query, params) = find_usages_query(&ctx.project_id, symbol_id, offset, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn find_caller_ids(
    ctx: &Context,
    symbol_id: &str,
    limit: usize,
) -> anyhow::Result<Vec<String>> {
    with_required_core_graph(ctx, |client| {
        let (query, params) = find_caller_ids_query(&ctx.project_id, symbol_id, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows
            .iter()
            .filter_map(|row| row_string_owned(row, &["id"]))
            .collect())
    })
}

pub fn find_usage_ids(ctx: &Context, symbol_id: &str, limit: usize) -> anyhow::Result<Vec<String>> {
    with_required_core_graph(ctx, |client| {
        let (query, params) = find_usage_ids_query(&ctx.project_id, symbol_id, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows
            .iter()
            .filter_map(|row| row_string_owned(row, &["id"]))
            .collect())
    })
}

pub fn find_callers_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    if symbol_ids.is_empty() {
        return Ok(vec![]);
    }
    with_required_core_graph(ctx, |client| {
        let (query, params) = find_callers_batch_query(&ctx.project_id, symbol_ids, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn find_caller_ids_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<Vec<String>> {
    if symbol_ids.is_empty() {
        return Ok(vec![]);
    }
    with_required_core_graph(ctx, |client| {
        let (query, params) = find_caller_ids_batch_query(&ctx.project_id, symbol_ids, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows
            .iter()
            .filter_map(|row| row_string_owned(row, &["id"]))
            .collect())
    })
}

pub fn find_callees_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    if symbol_ids.is_empty() {
        return Ok(vec![]);
    }
    with_required_core_graph(ctx, |client| {
        let (query, params) = find_callees_batch_query(&ctx.project_id, symbol_ids, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn find_callee_ids_batch(
    ctx: &Context,
    symbol_ids: &[String],
    limit: usize,
) -> anyhow::Result<Vec<String>> {
    if symbol_ids.is_empty() {
        return Ok(vec![]);
    }
    with_required_core_graph(ctx, |client| {
        let (query, params) = find_callee_ids_batch_query(&ctx.project_id, symbol_ids, limit);
        let rows = client.query(&query, Some(params))?;
        Ok(rows
            .iter()
            .filter_map(|row| row_string_owned(row, &["id"]))
            .collect())
    })
}

pub fn get_imports(ctx: &Context, file_path: &str) -> anyhow::Result<Vec<GraphResult>> {
    with_required_core_graph(ctx, |client| {
        let (query, params) = get_imports_query(&ctx.project_id, file_path);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}

pub fn blast_radius(
    ctx: &Context,
    symbol_id: &str,
    depth: usize,
) -> anyhow::Result<Vec<GraphResult>> {
    with_required_core_graph(ctx, |client| {
        let query = blast_radius_query(depth, MAX_GRAPH_LIMIT);
        let params = typed_query::string_params(&[("project", &ctx.project_id), ("id", symbol_id)]);
        let rows = client.query(&query, Some(params))?;
        Ok(rows.iter().map(row_to_graph_result).collect())
    })
}
