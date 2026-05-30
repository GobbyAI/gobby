use std::collections::HashMap;

use crate::graph::typed_query;

pub(super) fn report_node_type_case(alias: &str) -> String {
    format!(
        "CASE \
          WHEN {alias}:CodeFile THEN 'file' \
          WHEN {alias}:CodeModule THEN 'module' \
          WHEN {alias}:CodeSymbol THEN coalesce({alias}.kind, 'symbol') \
          WHEN {alias}:UnresolvedCallee THEN 'unresolved' \
          WHEN {alias}:ExternalSymbol THEN 'external' \
          ELSE 'node' \
        END"
    )
}

fn report_node_id_expr(alias: &str) -> String {
    format!("coalesce({alias}.id, {alias}.path, {alias}.name)")
}

fn report_node_name_expr(alias: &str) -> String {
    format!("coalesce({alias}.name, {alias}.path, {alias}.id)")
}

pub(super) fn report_node_counts_query(project_id: &str) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (n {{project: $project}}) \
             WHERE n:CodeFile OR n:CodeSymbol OR n:CodeModule OR n:UnresolvedCallee OR n:ExternalSymbol \
             RETURN {} AS name, count(n) AS count",
            report_node_type_case("n")
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub(super) fn report_code_edge_counts_query(project_id: &str) -> (String, HashMap<String, String>) {
    (
        "MATCH (source {project: $project})-[r]->(target {project: $project}) \
         WHERE type(r) IN ['DEFINES', 'IMPORTS', 'CALLS'] \
         RETURN type(r) AS name, count(r) AS count"
            .to_string(),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub(super) fn report_hotspots_query(
    project_id: &str,
    node_class: &str,
    top_n: usize,
) -> (String, HashMap<String, String>) {
    let predicate = match node_class {
        "file" => "n:CodeFile",
        "module" => "n:CodeModule",
        _ => "n:CodeSymbol",
    };
    let limit = top_n.max(1);
    (
        format!(
            "MATCH (n {{project: $project}}) \
             WHERE {predicate} \
             OPTIONAL MATCH (n)-[out]->(out_target {{project: $project}}) \
             WHERE type(out) IN ['DEFINES', 'IMPORTS', 'CALLS'] \
               AND (out_target:CodeFile OR out_target:CodeSymbol OR out_target:CodeModule OR out_target:UnresolvedCallee OR out_target:ExternalSymbol) \
             WITH n, count(out) AS outgoing \
             OPTIONAL MATCH (in_source {{project: $project}})-[inc]->(n) \
             WHERE type(inc) IN ['DEFINES', 'IMPORTS', 'CALLS'] \
               AND (in_source:CodeFile OR in_source:CodeSymbol OR in_source:CodeModule OR in_source:UnresolvedCallee OR in_source:ExternalSymbol) \
             WITH n, outgoing, count(inc) AS incoming \
             WITH n, outgoing, incoming, outgoing + incoming AS degree \
             WHERE degree > 0 \
             RETURN {} AS id, {} AS name, {} AS node_type, degree, incoming, outgoing, coalesce(n.file_path, n.path) AS file_path \
             ORDER BY degree DESC, name ASC, id ASC \
             LIMIT {limit}",
            report_node_id_expr("n"),
            report_node_name_expr("n"),
            report_node_type_case("n")
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub(super) fn report_incoming_call_hotspots_query(
    project_id: &str,
    top_n: usize,
) -> (String, HashMap<String, String>) {
    let limit = top_n.max(1);
    (
        format!(
            "MATCH (:CodeSymbol {{project: $project}})-[r:CALLS]->(n:CodeSymbol {{project: $project}}) \
             WITH n, count(r) AS incoming \
             WHERE incoming > 0 \
             RETURN n.id AS id, coalesce(n.name, n.id) AS name, {} AS node_type, incoming AS degree, incoming, 0 AS outgoing, n.file_path AS file_path \
             ORDER BY degree DESC, name ASC, id ASC \
             LIMIT {limit}",
            report_node_type_case("n")
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub(super) fn report_target_frequencies_query(
    project_id: &str,
    target_type: &str,
    top_n: usize,
) -> (String, HashMap<String, String>) {
    let target_label = if target_type == "external" {
        "ExternalSymbol"
    } else {
        "UnresolvedCallee"
    };
    let limit = top_n.max(1);
    (
        format!(
            "MATCH (:CodeSymbol {{project: $project}})-[r:CALLS]->(target:{target_label} {{project: $project}}) \
             RETURN target.id AS id, coalesce(target.name, target.id) AS name, count(r) AS count \
             ORDER BY count DESC, name ASC, id ASC \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub(super) fn report_bridge_edges_query(project_id: &str) -> (String, HashMap<String, String>) {
    (
        "MATCH (source)-[r:RELATES_TO_CODE]->(target:CodeSymbol {project: $project}) \
         RETURN coalesce(source.id, source.uuid, source.name) AS source_id, \
                target.id AS target_symbol_id, \
                'RELATES_TO_CODE' AS relation, \
                r.provenance AS provenance, \
                r.confidence AS confidence, \
                coalesce(r.source_system, 'gobby-memory') AS source_system, \
                r.source_file_path AS source_file_path, \
                r.source_line AS source_line, \
                r.source_symbol_id AS source_symbol_id, \
                r.matching_method AS matching_method"
            .to_string(),
        typed_query::string_params(&[("project", project_id)]),
    )
}
