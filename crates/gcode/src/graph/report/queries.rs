use std::collections::HashMap;

use crate::graph::typed_query;

const CODE_EDGE_REL_PATTERN: &str = "DEFINES|IMPORTS|CALLS|INHERITS|EXTENDS|IMPLEMENTS";

/// Admission test over one grouped target row. `sym`, `p`, `h`, `vp`, `vh` are
/// the target's `CodeSymbol` flag, own path and hash, and stamped path and hash.
/// Non-symbol targets always pass; symbols pass by their own version or by a
/// stamped callee version. The test runs once per distinct target version, not
/// once per edge.
const ADMITTED_TARGET: &str = "NOT sym \
     OR h IN coalesce($admitted_map[p], []) \
     OR vh IN coalesce($endpoint_map[vp], [])";

/// Edge-version check for rows that the seed does not imply (`chk`): the edge's own
/// hash `eh` must be an admitted version of its file `ef`. Other rows pass.
const ADMITTED_EDGE: &str = "CASE WHEN chk THEN eh IN coalesce($admitted_map[ef], []) ELSE true END";

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

/// One row per candidate edge: `src`, `ty`, `t`, the target's symbol flag, own path
/// and hash, stamped path and hash, then `chk`, `ef` and `eh` (see `ADMITTED_EDGE`).
fn edge_columns(chk: &str, ef: &str, eh: &str) -> String {
    format!(
        "src, type(r) AS ty, t, (t:CodeSymbol) AS sym, t.file_path AS p, \
         t.file_content_hash AS h, t.version_file_path AS vp, \
         t.version_content_hash AS vh, {chk} AS chk, {ef} AS ef, {eh} AS eh"
    )
}

/// Edges seeded by an admitted version. A file-anchored edge (DEFINES, IMPORTS)
/// starts at an admitted file version; a symbol-anchored edge starts at an admitted
/// symbol version. Both match `r.content_hash` to the seed, so they need no check.
/// The last branch takes a symbol's edges that name another file (`r.file`); those
/// need the `ef`/`eh` check.
fn seeded_edge_rows() -> String {
    let seeded = edge_columns("false", "NULL", "NULL");
    let foreign = edge_columns("true", "r.file", "r.content_hash");
    format!(
        "UNWIND $admitted_pairs AS v \
         MATCH (src:CodeFile {{project: $project, path: v.p}})-[r:{CODE_EDGE_REL_PATTERN}]->(t {{project: $project}}) \
         WHERE r.content_hash = v.h AND coalesce(r.file, src.path) = src.path \
         RETURN {seeded} \
         UNION ALL \
         UNWIND $admitted_pairs AS v \
         MATCH (src:CodeSymbol {{project: $project, file_path: v.p}})-[r:{CODE_EDGE_REL_PATTERN}]->(t {{project: $project}}) \
         WHERE src.file_content_hash = v.h AND r.content_hash = v.h \
           AND coalesce(r.file, src.file_path) = src.file_path \
         RETURN {seeded} \
         UNION ALL \
         UNWIND $admitted_pairs AS v \
         MATCH (src:CodeSymbol {{project: $project, file_path: v.p}})-[r:{CODE_EDGE_REL_PATTERN}]->(t {{project: $project}}) \
         WHERE src.file_content_hash = v.h AND r.file IS NOT NULL AND r.file <> src.file_path \
         RETURN {foreign}"
    )
}

/// Edges whose source is a placeholder (module, unresolved callee, external symbol).
/// The source is not seeded, so each row is checked by its own edge version.
fn placeholder_edge_rows() -> String {
    let body = edge_columns("true", "coalesce(r.file, src.path)", "r.content_hash");
    ["CodeModule", "UnresolvedCallee", "ExternalSymbol"]
        .map(|label| {
            format!(
                "MATCH (src:{label} {{project: $project}})-[r:{CODE_EDGE_REL_PATTERN}]->(t {{project: $project}}) \
                 RETURN {body}"
            )
        })
        .join(" UNION ALL ")
}

/// Every admitted candidate edge row (see `edge_columns`).
fn admitted_edge_rows() -> String {
    format!("{} UNION ALL {}", seeded_edge_rows(), placeholder_edge_rows())
}

/// Admitted nodes by identity: files by admitted path, symbols by admitted
/// version, and placeholders (modules, unresolved callees, external symbols)
/// that an admitted edge reaches.
pub(super) fn report_node_counts_query(project_id: &str) -> (String, HashMap<String, String>) {
    let edges = admitted_edge_rows();
    let placeholder_edges = placeholder_edge_rows();
    (
        format!(
            "MATCH (n:CodeFile {{project: $project}}) \
             WHERE n.path IN $admitted_paths \
             RETURN {case} AS name, count(n) AS count \
             UNION ALL \
             UNWIND $admitted_pairs AS v \
             MATCH (n:CodeSymbol {{project: $project, file_path: v.p}}) \
             WHERE n.file_content_hash = v.h \
             RETURN {case} AS name, count(n) AS count \
             UNION ALL \
             CALL {{ \
               CALL {{ {edges} }} \
               WITH t AS n, sym, chk, ef, eh WHERE NOT sym AND ({ADMITTED_EDGE}) RETURN n \
               UNION ALL \
               CALL {{ {placeholder_edges} }} \
               WITH src AS n, sym, chk, ef, eh, p, h, vp, vh \
               WHERE ({ADMITTED_EDGE}) AND ({ADMITTED_TARGET}) RETURN n \
             }} \
             WITH DISTINCT n \
             WHERE n:CodeModule OR n:UnresolvedCallee OR n:ExternalSymbol \
             RETURN {case} AS name, count(n) AS count",
            case = report_node_type_case("n")
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub(super) fn report_code_edge_counts_query(project_id: &str) -> (String, HashMap<String, String>) {
    let edges = admitted_edge_rows();
    (
        format!(
            "CALL {{ {edges} }} \
             WITH ty, sym, p, h, vp, vh, chk, ef, eh, count(*) AS c \
             WHERE ({ADMITTED_EDGE}) AND ({ADMITTED_TARGET}) \
             RETURN ty AS name, sum(c) AS count"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

/// Binds the admission maps once per query. A per-row `$admitted_map[...]`
/// parameter lookup timed out on large graphs; a bound variable does not.
const BIND_ADMISSION_MAPS: &str = "WITH $admitted_map AS am, $endpoint_map AS em";

/// Imports the bound maps into a `CALL {}` branch. The engine accepts only simple
/// variable references there, so every union branch imports them again.
const IMPORT_ADMISSION_MAPS: &str = "WITH am, em";

/// Admission of one endpoint in map form: only CodeSymbol nodes carry a file
/// version, checked against the admitted and endpoint version maps.
fn admitted_endpoint(alias: &str) -> String {
    format!(
        "(NOT {alias}:CodeSymbol \
         OR {alias}.file_content_hash IN coalesce(am[{alias}.file_path], []) \
         OR {alias}.version_content_hash IN coalesce(em[{alias}.version_file_path], []))"
    )
}

/// `limit` of `None` returns every ranked node of the class.
///
/// Degree is the sum of an outgoing and an incoming count per node, computed in
/// one union so only the ranked rows return. A result set above the engine's
/// 10000-row cap is truncated silently, so per-direction rows cannot be returned
/// in full.
pub(super) fn report_hotspots_query(
    project_id: &str,
    node_class: &str,
    limit: Option<usize>,
) -> (String, HashMap<String, String>) {
    let label = match node_class {
        "file" => "CodeFile",
        "module" => "CodeModule",
        _ => "CodeSymbol",
    };
    let limit_clause = limit.map_or_else(String::new, |limit| format!(" LIMIT {}", limit.max(1)));
    let node_admitted = admitted_endpoint("n");
    let out_admitted = admitted_endpoint("out_target");
    let in_admitted = admitted_endpoint("in_source");
    (
        format!(
            "{BIND_ADMISSION_MAPS} \
             CALL {{ \
             {IMPORT_ADMISSION_MAPS} \
             MATCH (n:{label} {{project: $project}})-[out:{CODE_EDGE_REL_PATTERN}]->(out_target {{project: $project}}) \
             WHERE (out_target:CodeFile OR out_target:CodeSymbol OR out_target:CodeModule OR out_target:UnresolvedCallee OR out_target:ExternalSymbol) \
               AND out.content_hash IN coalesce(am[coalesce(out.file, startNode(out).path)], []) \
               AND {node_admitted} AND {out_admitted} \
             WITH n, count(out) AS outgoing \
             RETURN n AS node, outgoing, 0 AS incoming \
             UNION ALL \
             {IMPORT_ADMISSION_MAPS} \
             MATCH (in_source {{project: $project}})-[inc:{CODE_EDGE_REL_PATTERN}]->(n:{label} {{project: $project}}) \
             WHERE (in_source:CodeFile OR in_source:CodeSymbol OR in_source:CodeModule OR in_source:UnresolvedCallee OR in_source:ExternalSymbol) \
               AND inc.content_hash IN coalesce(am[coalesce(inc.file, startNode(inc).path)], []) \
               AND {in_admitted} AND {node_admitted} \
             WITH n, count(inc) AS incoming \
             RETURN n AS node, 0 AS outgoing, incoming \
             }} \
             WITH node, sum(outgoing) AS outgoing, sum(incoming) AS incoming \
             WITH node, outgoing, incoming, outgoing + incoming AS degree \
             WHERE degree > 0 \
             RETURN {} AS id, {} AS name, {} AS node_type, degree, incoming, outgoing, coalesce(node.file_path, node.path) AS file_path \
             ORDER BY degree DESC, name ASC, id ASC{limit_clause}",
            report_node_id_expr("node"),
            report_node_name_expr("node"),
            report_node_type_case("node")
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

/// A CALLS edge seeded by an admitted caller version: a same-file edge by the
/// caller's version, or a foreign-file edge (`r.file`) by its own version.
const SEEDED_CALL_EDGE: &str = "src.file_content_hash = v.h \
     AND ((r.content_hash = v.h AND coalesce(r.file, src.file_path) = src.file_path) \
          OR (r.file IS NOT NULL AND r.file <> src.file_path AND r.content_hash IN coalesce(am[r.file], [])))";

pub(super) fn report_incoming_call_hotspots_query(
    project_id: &str,
    top_n: usize,
) -> (String, HashMap<String, String>) {
    let limit = top_n.max(1);
    let node_admitted = admitted_endpoint("n");
    (
        format!(
            "{BIND_ADMISSION_MAPS} \
             UNWIND $admitted_pairs AS v \
             MATCH (src:CodeSymbol {{project: $project, file_path: v.p}})-[r:CALLS]->(n:CodeSymbol {{project: $project}}) \
             WHERE {SEEDED_CALL_EDGE} AND {node_admitted} \
             WITH n, count(r) AS incoming \
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
            "{BIND_ADMISSION_MAPS} \
             UNWIND $admitted_pairs AS v \
             MATCH (src:CodeSymbol {{project: $project, file_path: v.p}})-[r:CALLS]->(target:{target_label} {{project: $project}}) \
             WHERE {SEEDED_CALL_EDGE} \
             RETURN target.id AS id, coalesce(target.name, target.id) AS name, count(r) AS count \
             ORDER BY count DESC, name ASC, id ASC \
             LIMIT {limit}"
        ),
        typed_query::string_params(&[("project", project_id)]),
    )
}

pub(super) fn report_bridge_edges_query(project_id: &str) -> (String, HashMap<String, String>) {
    (
        "UNWIND $admitted_pairs AS v \
         MATCH (target:CodeSymbol {project: $project, file_path: v.p})<-[r:RELATES_TO_CODE]-(source) \
         WHERE target.file_content_hash = v.h \
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
