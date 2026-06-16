use std::collections::HashSet;

use crate::graph::typed_query;
use crate::models::{GraphResult, ProjectionProvenance};
use gobby_core::falkor::Row;

use super::super::payload::{row_string_owned, row_to_projection_metadata, row_usize};

pub(super) const CALL_TARGET_PREDICATE: &str =
    "target:CodeSymbol OR target:UnresolvedCallee OR target:ExternalSymbol";
pub(super) const NEIGHBOR_PREDICATE: &str =
    "neighbor:CodeSymbol OR neighbor:UnresolvedCallee OR neighbor:ExternalSymbol";
pub(super) const TARGET_TYPE_CASE: &str = "CASE \
     WHEN target:CodeSymbol THEN coalesce(target.kind, 'function') \
     WHEN target:ExternalSymbol THEN 'external' \
     ELSE 'unresolved' \
     END";
pub(super) const NEIGHBOR_TYPE_CASE: &str = "CASE \
     WHEN neighbor:CodeSymbol THEN coalesce(neighbor.kind, 'function') \
     WHEN neighbor:ExternalSymbol THEN 'external' \
     ELSE 'unresolved' \
     END";
pub(super) const CONFIDENCE_LABEL_CASE: &str = "CASE \
     WHEN 'AMBIGUOUS' IN provenances THEN 'AMBIGUOUS' \
     WHEN 'INFERRED' IN provenances THEN 'INFERRED' \
     ELSE 'EXTRACTED' \
     END";
pub(super) const NODE_TYPE_CASE: &str = "CASE \
     WHEN n:CodeFile THEN 'file' \
     WHEN n:CodeModule THEN 'module' \
     WHEN n:CodeSymbol THEN coalesce(n.kind, 'function') \
     WHEN n:ExternalSymbol THEN 'external' \
     ELSE 'unresolved' \
     END";
pub(super) const LINK_METADATA_RETURN: &str = "r.provenance AS provenance, \
     r.confidence AS confidence, \
     r.source_system AS source_system, \
     r.source_file_path AS metadata_source_file_path, \
     r.source_line AS source_line, \
     r.source_symbol_id AS source_symbol_id, \
     r.matching_method AS matching_method";
pub(super) const MAX_GRAPH_LIMIT: usize = 100;
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
        confidence: row
            .get("confidence_label")
            .or_else(|| row.get("provenance"))
            .and_then(|v| v.as_str())
            .and_then(ProjectionProvenance::from_wire_value)
            .unwrap_or_default(),
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
pub(super) fn clamp_limit(limit: usize) -> usize {
    typed_query::clamp_limit(limit, MAX_GRAPH_LIMIT)
}

pub(super) fn clamp_offset(offset: usize) -> usize {
    typed_query::clamp_offset(offset, MAX_GRAPH_LIMIT)
}
pub(in crate::graph::code_graph) fn dedupe_limited_blast_rows(
    mut rows: Vec<Row>,
    limit: usize,
) -> Vec<Row> {
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

pub(super) fn count_from_rows(rows: &[Row]) -> usize {
    rows.first()
        .and_then(|r| r.get("cnt"))
        .and_then(|v| {
            v.as_u64()
                .or_else(|| v.as_i64().and_then(|value| value.try_into().ok()))
        })
        .and_then(|value| usize::try_from(value).ok())
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn graph_result_confidence_defaults_to_extracted_without_metadata() {
        let row = Row::from([
            ("caller_id".to_string(), json!("caller-1")),
            ("caller_name".to_string(), json!("run")),
            ("file".to_string(), json!("src/lib.rs")),
            ("line".to_string(), json!(12)),
        ]);

        let result = row_to_graph_result(&row);

        assert_eq!(result.confidence, ProjectionProvenance::Extracted);
        assert!(result.metadata.is_none());
    }

    #[test]
    fn graph_result_confidence_uses_provenance_label_with_metadata_score() {
        let row = Row::from([
            ("source_id".to_string(), json!("caller-1")),
            ("source_name".to_string(), json!("run")),
            ("file".to_string(), json!("src/lib.rs")),
            ("line".to_string(), json!(12)),
            ("rel_type".to_string(), json!("CALLS")),
            ("provenance".to_string(), json!("INFERRED")),
            ("confidence".to_string(), json!(0.72)),
            ("source_system".to_string(), json!("semantic")),
        ]);

        let result = row_to_graph_result(&row);

        assert_eq!(result.confidence, ProjectionProvenance::Inferred);
        assert_eq!(
            result
                .metadata
                .as_ref()
                .and_then(|metadata| metadata.confidence),
            Some(0.72)
        );
    }
}
