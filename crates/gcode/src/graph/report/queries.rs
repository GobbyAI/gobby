use std::collections::HashMap;

use crate::graph::typed_query;

const CODE_EDGE_REL_TYPES: &str =
    "['DEFINES', 'IMPORTS', 'CALLS', 'INHERITS', 'EXTENDS', 'IMPLEMENTS']";
const CODE_EDGE_REL_PATTERN: &str = "DEFINES|IMPORTS|CALLS|INHERITS|EXTENDS|IMPLEMENTS";

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
        format!(
            "MATCH (source {{project: $project}})-[r]->(target {{project: $project}}) \
             WHERE type(r) IN {CODE_EDGE_REL_TYPES} \
             RETURN type(r) AS name, count(r) AS count"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub(super) fn report_hotspots_query(
    project_id: &str,
    node_class: &str,
    top_n: usize,
) -> (String, HashMap<String, String>) {
    let label = match node_class {
        "file" => "CodeFile",
        "module" => "CodeModule",
        _ => "CodeSymbol",
    };
    let limit = top_n.max(1);
    (
        // Compute in/out degree with two edge-driven aggregations combined by
        // node, instead of seeding every node and double-expanding it. The
        // node-driven form carried all ~70k symbols (including zero-degree
        // ones) through the second OPTIONAL MATCH and timed out server-side on
        // large graphs; the edge-driven form only touches nodes that have
        // edges and returns in well under a second (task #17679).
        format!(
            "CALL {{ \
             MATCH (n:{label} {{project: $project}})-[out:{CODE_EDGE_REL_PATTERN}]->(out_target {{project: $project}}) \
             WHERE (out_target:CodeFile OR out_target:CodeSymbol OR out_target:CodeModule OR out_target:UnresolvedCallee OR out_target:ExternalSymbol) \
             RETURN n AS node, count(out) AS outgoing, 0 AS incoming \
             UNION ALL \
             MATCH (in_source {{project: $project}})-[inc:{CODE_EDGE_REL_PATTERN}]->(n:{label} {{project: $project}}) \
             WHERE (in_source:CodeFile OR in_source:CodeSymbol OR in_source:CodeModule OR in_source:UnresolvedCallee OR in_source:ExternalSymbol) \
             RETURN n AS node, 0 AS outgoing, count(inc) AS incoming \
             }} \
             WITH node, sum(outgoing) AS outgoing, sum(incoming) AS incoming \
             WITH node, outgoing, incoming, outgoing + incoming AS degree \
             WHERE degree > 0 \
             RETURN {} AS id, {} AS name, {} AS node_type, degree, incoming, outgoing, coalesce(node.file_path, node.path) AS file_path \
             ORDER BY degree DESC, name ASC, id ASC \
             LIMIT {limit}",
            report_node_id_expr("node"),
            report_node_name_expr("node"),
            report_node_type_case("node")
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hotspots_query_uses_label_scoped_seed_and_typed_edges() {
        let (query, params) = report_hotspots_query("proj-1", "symbol", 6);

        // Seeds from the :CodeSymbol(project) index instead of scanning every
        // project node and filtering by label afterwards, which forced a
        // full-graph scan and timed out on large graphs (task #17679).
        assert!(
            query.contains("MATCH (n:CodeSymbol {project: $project})"),
            "expected label-scoped seed, got: {query}"
        );
        assert!(
            !query.contains("WHERE n:CodeSymbol"),
            "label-less MATCH + WHERE label filter regressed: {query}"
        );

        // Traverses the DEFINES/IMPORTS/CALLS/INHERITS/EXTENDS/IMPLEMENTS
        // relationship-type matrices directly instead of expanding every edge
        // and filtering by type().
        assert!(
            query.contains("-[out:DEFINES|IMPORTS|CALLS|INHERITS|EXTENDS|IMPLEMENTS]->"),
            "expected typed outgoing traversal, got: {query}"
        );
        assert!(
            query.contains("-[inc:DEFINES|IMPORTS|CALLS|INHERITS|EXTENDS|IMPLEMENTS]->"),
            "expected typed incoming traversal, got: {query}"
        );
        assert!(
            !query.contains("type(out) IN"),
            "untyped-then-type()-filter anti-pattern regressed: {query}"
        );
        assert!(
            !query.contains("type(inc) IN"),
            "untyped incoming filter regressed: {query}"
        );

        // Degree is computed by two edge-driven aggregations combined per node,
        // not by seeding every node and double-expanding it (which timed out).
        assert!(
            query.contains("CALL {") && query.contains("UNION ALL"),
            "expected edge-driven CALL/UNION ALL aggregation, got: {query}"
        );
        assert!(
            query.contains("sum(outgoing)") && query.contains("sum(incoming)"),
            "expected per-node re-aggregation, got: {query}"
        );
        assert!(
            !query.contains("OPTIONAL MATCH"),
            "node-driven double-expansion regressed: {query}"
        );

        // top_n is still pushed down as the LIMIT.
        assert!(
            query.contains("LIMIT 6"),
            "expected pushed-down limit, got: {query}"
        );
        assert!(params.contains_key("project"));
    }

    #[test]
    fn report_edge_patterns_include_inheritance_rels() {
        for rel in ["INHERITS", "EXTENDS", "IMPLEMENTS"] {
            assert!(
                CODE_EDGE_REL_TYPES.contains(rel),
                "CODE_EDGE_REL_TYPES missing {rel}: {CODE_EDGE_REL_TYPES}"
            );
            assert!(
                CODE_EDGE_REL_PATTERN.contains(rel),
                "CODE_EDGE_REL_PATTERN missing {rel}: {CODE_EDGE_REL_PATTERN}"
            );
        }
        let (query, _) = report_code_edge_counts_query("proj-1");
        for rel in ["INHERITS", "EXTENDS", "IMPLEMENTS"] {
            assert!(
                query.contains(rel),
                "edge-count query missing {rel}: {query}"
            );
        }
    }

    #[test]
    fn hotspots_query_scopes_seed_label_by_node_class() {
        let (file_query, _) = report_hotspots_query("proj-1", "file", 3);
        assert!(file_query.contains("MATCH (n:CodeFile {project: $project})"));

        let (module_query, _) = report_hotspots_query("proj-1", "module", 3);
        assert!(module_query.contains("MATCH (n:CodeModule {project: $project})"));
    }
}
