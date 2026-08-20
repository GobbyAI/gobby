use std::collections::HashMap;

use crate::graph::typed_query;

use super::support::{
    CALL_TARGET_PREDICATE, LINK_METADATA_RETURN, NEIGHBOR_PREDICATE, NEIGHBOR_TYPE_CASE,
    NODE_TYPE_CASE, TARGET_TYPE_CASE, anchored_call_node, clamp_limit,
};

pub(super) fn project_overview_files_query(
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

pub(super) fn project_overview_imports_query(
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

pub(super) fn project_overview_defines_query(
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

pub(super) fn project_overview_calls_query(
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

pub(super) fn file_symbols_query(
    project_id: &str,
    file_path: &str,
) -> (String, HashMap<String, String>) {
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

pub(in crate::graph::code_graph) fn file_calls_query(
    project_id: &str,
    file_path: &str,
) -> (String, HashMap<String, String>) {
    let columns = format!(
        "RETURN source.id AS source_id, source.name AS source_name, \
                coalesce(source.kind, 'function') AS source_type, \
                source.kind AS source_kind, source.file_path AS source_file_path, \
                source.line_start AS source_line_start, source.signature AS source_signature, \
                target.id AS target_id, target.name AS target_name, \
                {TARGET_TYPE_CASE} AS target_type, target.kind AS target_kind, \
                target.file_path AS target_file_path, \
                target.line_start AS target_line_start, target.signature AS target_signature, \
                source.id AS source, target.id AS target, 'CALLS' AS type, r.line AS line, \
                {LINK_METADATA_RETURN}"
    );
    // One `source.file_path = $path OR target.file_path = $path` predicate
    // cannot use the CodeSymbol file_path index on either endpoint, so it
    // walks every CALLS edge in the project. Two index-anchored branches cover
    // the same edge set: calls leaving the file, then calls arriving from
    // other files (the `WITH` keeps the planner expanding from the bound
    // target instead of re-scanning every project symbol).
    (
        format!(
            "MATCH (source:CodeSymbol {{project: $project, file_path: $path}})-[r:CALLS]->(target {{project: $project}}) \
             WHERE ({CALL_TARGET_PREDICATE}) \
             {columns} \
             UNION ALL \
             MATCH (target:CodeSymbol {{project: $project, file_path: $path}}) \
             WITH target \
             MATCH (source:CodeSymbol {{project: $project}})-[r:CALLS]->(target) \
             WHERE source.file_path IS NULL OR source.file_path <> $path \
             {columns}"
        ),
        typed_query::string_params(&[("project", project_id), ("path", file_path)]),
    )
}

pub(super) fn symbol_neighbors_query(
    project_id: &str,
    symbol_id: &str,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let anchor = anchored_call_node("center", "$id");
    (
        format!(
            "{anchor} \
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

pub(super) fn blast_radius_center_query(
    project_id: &str,
    symbol_id: &str,
) -> (String, HashMap<String, String>) {
    let anchor = anchored_call_node("n", "$id");
    (
        format!(
            "{anchor} \
             RETURN n.id AS id, n.name AS name, {NODE_TYPE_CASE} AS type, \
                    n.kind AS kind, n.file_path AS file_path \
             LIMIT 1"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub(super) fn blast_radius_file_call_query(
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

pub(in crate::graph::code_graph) fn blast_radius_file_import_query(
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn file_calls_query_anchors_both_directions_on_the_file_path_index() {
        let (query, _) = file_calls_query("project-1", "src/lib.rs");

        assert!(query.starts_with(
            "MATCH (source:CodeSymbol {project: $project, file_path: $path})-[r:CALLS]->(target {project: $project})"
        ));
        assert!(query.contains(
            "UNION ALL MATCH (target:CodeSymbol {project: $project, file_path: $path}) WITH target \
             MATCH (source:CodeSymbol {project: $project})-[r:CALLS]->(target) \
             WHERE source.file_path IS NULL OR source.file_path <> $path"
        ));
        assert!(!query.contains("source.file_path = $path OR"));
        assert_eq!(query.matches("RETURN source.id AS source_id").count(), 2);
    }

    #[test]
    fn symbol_anchored_payload_queries_resolve_ids_through_labeled_indexes() {
        let (neighbors, _) = symbol_neighbors_query("project-1", "symbol-1", 10);
        let (center, _) = blast_radius_center_query("project-1", "symbol-1");

        for query in [&neighbors, &center] {
            assert!(
                query.starts_with(
                    "OPTIONAL MATCH (anchor_symbol:CodeSymbol {id: $id, project: $project})"
                ),
                "{query}"
            );
        }
        assert!(neighbors.contains(
            "AS center WHERE center IS NOT NULL MATCH (center)-[r:CALLS]-(neighbor {project: $project})"
        ));
        assert!(center.contains("AS n WHERE n IS NOT NULL RETURN n.id AS id"));
        assert!(!neighbors.contains("(center {id: $id, project: $project})"));
        assert!(!center.contains("(n {id: $id, project: $project})"));
    }
}
