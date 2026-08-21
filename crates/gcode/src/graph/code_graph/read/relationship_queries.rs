use std::collections::HashMap;

use crate::graph::typed_query;

use super::support::{
    CALL_TARGET_PREDICATE, CONFIDENCE_LABEL_CASE, LINK_METADATA_RETURN, anchored_call_node,
    clamp_limit, clamp_offset,
};

pub(crate) fn find_callers_query(
    project_id: &str,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let offset = clamp_offset(offset);
    let limit = clamp_limit(limit);
    let anchor = anchored_call_node("target", "$id");
    (
        format!(
            "{anchor} \
             MATCH (caller:CodeSymbol {{project: $project}})-[r:CALLS]->(target) \
             WITH caller, collect(coalesce(r.provenance, 'EXTRACTED')) AS provenances \
             WITH caller, {CONFIDENCE_LABEL_CASE} AS confidence_label \
             RETURN caller.id AS caller_id, caller.name AS caller_name, \
                    caller.file_path AS file, caller.line_start AS line, \
                    confidence_label AS confidence_label \
             ORDER BY caller.id \
             SKIP {offset} LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub(crate) fn find_callees_query(
    project_id: &str,
    symbol_id: &str,
    offset: usize,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let offset = clamp_offset(offset);
    let limit = clamp_limit(limit);
    (
        format!(
            "MATCH (src:CodeSymbol {{project: $project, id: $id}})-[r:CALLS]->\
             (target {{project: $project}}) \
             WHERE ({CALL_TARGET_PREDICATE}) \
             WITH target, collect(coalesce(r.provenance, 'EXTRACTED')) AS provenances, \
                  min(r.file) AS file, min(r.line) AS line, \
                  CASE WHEN target:ExternalSymbol THEN 'external' \
                       WHEN target:UnresolvedCallee THEN 'unresolved' \
                       ELSE 'symbol' END AS node_kind \
             WITH target, file, line, node_kind, {CONFIDENCE_LABEL_CASE} AS confidence_label \
             RETURN target.id AS callee_id, target.name AS callee_name, \
                    file AS file, line AS line, confidence_label AS confidence_label, \
                    node_kind AS node_kind \
             ORDER BY target.id \
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
    let anchor = anchored_call_node("target", "$id");
    (
        format!(
            "{anchor} \
             MATCH (source:CodeSymbol {{project: $project}})-[r:CALLS]->(target) \
             RETURN source.id AS source_id, source.name AS source_name, \
                    'CALLS' AS rel_type, r.file AS file, r.line AS line, \
                    {LINK_METADATA_RETURN} \
             ORDER BY source.id, r.line, r.file, id(r) \
             SKIP {offset} LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub(super) fn find_caller_ids_query(
    project_id: &str,
    symbol_id: &str,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let anchor = anchored_call_node("target", "$id");
    (
        format!(
            "{anchor} \
             MATCH (caller:CodeSymbol {{project: $project}})-[:CALLS]->(target) \
             RETURN DISTINCT caller.id AS id \
             ORDER BY caller.id \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

pub(super) fn find_usage_ids_query(
    project_id: &str,
    symbol_id: &str,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let anchor = anchored_call_node("target", "$id");
    (
        format!(
            "{anchor} \
             MATCH (source:CodeSymbol {{project: $project}})-[:CALLS]->(target) \
             RETURN DISTINCT source.id AS id \
             ORDER BY source.id \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id), ("id", symbol_id)]),
    )
}

#[cfg(test)]
pub(crate) fn find_callers_batch_query(
    project_id: &str,
    symbol_ids: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let ids = typed_query::id_list_literal(symbol_ids);
    let anchor = anchored_call_node("target", "anchor_id");
    (
        format!(
            "UNWIND [{ids}] AS anchor_id \
             {anchor} \
             MATCH (caller:CodeSymbol {{project: $project}})-[r:CALLS]->(target) \
			 WITH caller, min(r.file) AS file, min(r.line) AS line, \
			      collect(coalesce(r.provenance, 'EXTRACTED')) AS provenances \
			 WITH caller, file, line, {CONFIDENCE_LABEL_CASE} AS confidence_label \
			 RETURN caller.id AS caller_id, caller.name AS caller_name, \
			        file AS file, line AS line, confidence_label AS confidence_label \
			 ORDER BY caller.id \
			 LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub(super) fn find_caller_ids_batch_query(
    project_id: &str,
    symbol_ids: &[String],
    limit: usize,
) -> (String, HashMap<String, String>) {
    let limit = clamp_limit(limit);
    let ids = typed_query::id_list_literal(symbol_ids);
    let anchor = anchored_call_node("target", "anchor_id");
    (
        format!(
            "UNWIND [{ids}] AS anchor_id \
             {anchor} \
             MATCH (caller:CodeSymbol {{project: $project}})-[:CALLS]->(target) \
             RETURN DISTINCT caller.id AS id \
             ORDER BY caller.id \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

#[cfg(test)]
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
			 WITH target, min(r.file) AS file, min(r.line) AS line, \
			      collect(coalesce(r.provenance, 'EXTRACTED')) AS provenances \
			 WITH target, file, line, {CONFIDENCE_LABEL_CASE} AS confidence_label \
			 RETURN target.id AS callee_id, target.name AS callee_name, \
			        file AS file, line AS line, confidence_label AS confidence_label \
			 ORDER BY target.id \
			 LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub(super) fn find_callee_ids_batch_query(
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

pub(crate) fn symbol_callee_edges_query(
    project_id: &str,
    symbol_ids: &[String],
) -> (String, HashMap<String, String>) {
    let ids = typed_query::id_list_literal(symbol_ids);
    (
        format!(
            "MATCH (source:CodeSymbol {{project: $project}})-[:CALLS]->(target:CodeSymbol {{project: $project}}) \
             WHERE source.id IN [{ids}] \
             RETURN DISTINCT source.id AS source_id, target.id AS target_id \
             ORDER BY source.id, target.id"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub(crate) fn symbol_path_steps_query(
    project_id: &str,
    symbol_ids: &[String],
) -> (String, HashMap<String, String>) {
    let ids = typed_query::id_list_literal(symbol_ids);
    (
        format!(
            "MATCH (symbol:CodeSymbol {{project: $project}}) \
             WHERE symbol.id IN [{ids}] \
             RETURN symbol.id AS symbol_id, \
                    coalesce(symbol.name, symbol.id) AS symbol_name, \
                    coalesce(symbol.file_path, '') AS file_path, \
                    coalesce(symbol.line_start, 0) AS line"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub(crate) fn get_imports_query(
    project_id: &str,
    file_path: &str,
    content_hash: &str,
) -> (String, HashMap<String, String>) {
    (
        "MATCH (f:CodeFile {path: $path, project: $project})\
         -[:IMPORTS {content_hash: $content_hash}]->(m:CodeModule) \
         RETURN m.name AS id, m.name AS module_name"
            .to_string(),
        typed_query::string_params(&[
            ("project", project_id),
            ("path", file_path),
            ("content_hash", content_hash),
        ]),
    )
}

pub(crate) fn resolve_external_call_target_query(
    project_id: &str,
    input: &str,
) -> (String, HashMap<String, String>) {
    let (module, member) = input.rsplit_once('.').unwrap_or(("", input));
    (
        "MATCH (target:ExternalSymbol {project: $project}) \
         WITH target, coalesce(target.module, target.external_module, '') AS module \
         WHERE target.id = $input \
            OR ($module <> '' AND target.name = $member AND module = $module) \
            OR target.name = $input \
         RETURN target.id AS id, target.name AS name, module AS module \
         ORDER BY CASE \
             WHEN target.id = $input THEN 0 \
             WHEN $module <> '' AND target.name = $member AND module = $module THEN 1 \
             ELSE 2 \
         END, module, target.name, target.id \
         LIMIT 11"
            .to_string(),
        typed_query::string_params(&[
            ("project", project_id),
            ("input", input),
            ("module", module),
            ("member", member),
        ]),
    )
}

pub(crate) fn blast_radius_query(depth: usize, limit: usize) -> String {
    let depth = depth.clamp(1, 5);
    let limit = clamp_limit(limit);
    let anchor = anchored_call_node("target", "$id");
    format!(
        "{anchor} \
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn callers_query_projects_confidence_without_edge_metadata_duplication() {
        let (query, _) = find_callers_query("project-1", "symbol-1", 0, 10);

        assert!(query.contains("confidence_label AS confidence_label"));
        assert!(query.contains("WHEN 'AMBIGUOUS' IN provenances THEN 'AMBIGUOUS'"));
        assert!(!query.contains("r.confidence AS confidence"));
    }

    #[test]
    fn callees_query_pages_outgoing_call_targets() {
        let (query, _) = find_callees_query("project-1", "symbol-1", 10, 20);

        assert!(query.contains("MATCH (src:CodeSymbol {project: $project, id: $id})"));
        assert!(query.contains(CALL_TARGET_PREDICATE));
        assert!(query.contains("ORDER BY target.id"));
        assert!(query.contains("SKIP 10 LIMIT 20"));
        assert!(query.contains("node_kind AS node_kind"));
        assert!(!query.contains("find_callees_batch"));
    }

    #[test]
    fn usages_query_projects_edge_confidence_and_metadata() {
        let (query, _) = find_usages_query("project-1", "symbol-1", 0, 10);

        assert!(query.contains("r.provenance AS provenance"));
        assert!(query.contains("r.confidence AS confidence"));
    }

    #[test]
    fn usages_query_orders_by_unique_edge_id_tiebreaker() {
        let (query, _) = find_usages_query("project-1", "symbol-1", 0, 10);

        // (source.id, r.line, r.file) is not unique across edges; without the
        // internal edge id tiebreaker SKIP/LIMIT paging can duplicate or drop
        // tied rows at page boundaries.
        assert!(query.contains("ORDER BY source.id, r.line, r.file, id(r)"));
    }

    #[test]
    fn batched_relationship_queries_project_confidence_label() {
        let ids = vec!["symbol-1".to_string()];

        let (callers_query, _) = find_callers_batch_query("project-1", &ids, 10);
        let (callees_query, _) = find_callees_batch_query("project-1", &ids, 10);

        assert!(callers_query.contains("confidence_label AS confidence_label"));
        assert!(callees_query.contains("confidence_label AS confidence_label"));
    }

    #[test]
    fn single_target_queries_anchor_on_labeled_id_indexes() {
        let queries = [
            find_callers_query("project-1", "symbol-1", 0, 10).0,
            find_usages_query("project-1", "symbol-1", 0, 10).0,
            find_caller_ids_query("project-1", "symbol-1", 10).0,
            find_usage_ids_query("project-1", "symbol-1", 10).0,
            blast_radius_query(3, 10),
        ];

        for query in queries {
            // An unlabeled `(target {id, project})` anchor forces an All Node
            // Scan; the three label-qualified probes each hit an index.
            assert!(
                query.starts_with(
                    "OPTIONAL MATCH (anchor_symbol:CodeSymbol {id: $id, project: $project})"
                ),
                "{query}"
            );
            assert!(
                query.contains("(anchor_unresolved:UnresolvedCallee {id: $id, project: $project})"),
                "{query}"
            );
            assert!(
                query.contains("(anchor_external:ExternalSymbol {id: $id, project: $project})"),
                "{query}"
            );
            assert!(
                query.contains(
                    "WITH coalesce(anchor_symbol, anchor_unresolved, anchor_external) AS target \
                     WHERE target IS NOT NULL"
                ),
                "{query}"
            );
            assert!(
                !query.contains("(target {id: $id, project: $project})"),
                "{query}"
            );
            assert!(!query.contains(CALL_TARGET_PREDICATE), "{query}");
        }
    }

    #[test]
    fn batched_caller_queries_unwind_ids_through_labeled_anchors() {
        let ids = vec!["symbol-1".to_string(), "symbol-2".to_string()];

        let (callers_query, _) = find_callers_batch_query("project-1", &ids, 10);
        let (caller_ids_query, _) = find_caller_ids_batch_query("project-1", &ids, 10);

        for query in [callers_query, caller_ids_query] {
            assert!(
                query.starts_with("UNWIND ['symbol-1', 'symbol-2'] AS anchor_id"),
                "{query}"
            );
            assert!(
                query.contains("(anchor_symbol:CodeSymbol {id: anchor_id, project: $project})"),
                "{query}"
            );
            assert!(
                query.contains("MATCH (caller:CodeSymbol {project: $project})-[")
                    && query.contains("]->(target)"),
                "{query}"
            );
            assert!(!query.contains("target.id IN"), "{query}");
        }
    }

    #[test]
    fn labeled_source_batches_keep_indexed_in_list_filters() {
        let ids = vec!["symbol-1".to_string()];

        let (callees_query, _) = find_callees_batch_query("project-1", &ids, 10);
        let (callee_ids_query, _) = find_callee_ids_batch_query("project-1", &ids, 10);

        // `src` is labeled, so `src.id IN [...]` is served by the CodeSymbol
        // id index and needs no UNWIND rewrite.
        for query in [callees_query, callee_ids_query] {
            assert!(
                query.contains("MATCH (src:CodeSymbol {project: $project})"),
                "{query}"
            );
            assert!(query.contains("src.id IN ['symbol-1']"), "{query}");
        }
    }
}
