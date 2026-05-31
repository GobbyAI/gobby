use std::collections::BTreeMap;

use gobby_core::falkor::Row;
use serde_json::Value;

use crate::models::{ProjectionMetadata, ProjectionProvenance};

use super::RELATES_TO_CODE;
use super::types::{BridgeEdgeHypothesis, GraphHotspot, TargetFrequency};

pub(super) fn rows_to_named_counts(rows: Vec<Row>) -> BTreeMap<String, usize> {
    rows.into_iter()
        .filter_map(|row| {
            let name = row_string(&row, &["name"])?;
            let count = row_usize(&row, &["count"]).unwrap_or(0);
            Some((name, count))
        })
        .collect()
}

pub(super) fn row_to_graph_hotspot(row: &Row) -> Option<GraphHotspot> {
    Some(GraphHotspot {
        id: row_string(row, &["id"])?,
        name: row_string(row, &["name"])?,
        node_type: row_string(row, &["node_type"]).unwrap_or_else(|| "node".to_string()),
        degree: row_usize(row, &["degree"]).unwrap_or(0),
        incoming: row_usize(row, &["incoming"]).unwrap_or(0),
        outgoing: row_usize(row, &["outgoing"]).unwrap_or(0),
        file_path: row_string(row, &["file_path"]),
    })
}

pub(super) fn row_to_target_frequency(row: &Row) -> Option<TargetFrequency> {
    Some(TargetFrequency {
        id: row_string(row, &["id"])?,
        name: row_string(row, &["name"])?,
        count: row_usize(row, &["count"]).unwrap_or(0),
    })
}

pub(super) fn row_to_bridge_edge_hypothesis(row: &Row) -> Option<BridgeEdgeHypothesis> {
    let source_id = row_string(row, &["source_id"])?;
    let target_symbol_id = row_string(row, &["target_symbol_id"])?;
    let relation = row_string(row, &["relation"]).unwrap_or_else(|| RELATES_TO_CODE.to_string());
    let source_system =
        row_string(row, &["source_system"]).unwrap_or_else(|| "gobby-memory".to_string());

    let mut metadata = ProjectionMetadata::new(
        row_string(row, &["provenance"])
            .and_then(|value| ProjectionProvenance::from_wire_value(&value))
            .unwrap_or(ProjectionProvenance::Inferred),
        source_system,
    );
    metadata.confidence = row_f64(row, &["confidence"]);
    metadata.source_file_path = row_string(row, &["source_file_path"]);
    metadata.source_line = row_usize(row, &["source_line"]);
    metadata.source_symbol_id = row_string(row, &["source_symbol_id"]);
    metadata.matching_method = row_string(row, &["matching_method"]);

    Some(BridgeEdgeHypothesis::new(
        source_id,
        target_symbol_id,
        relation,
        metadata,
    ))
}

fn row_string(row: &Row, keys: &[&str]) -> Option<String> {
    keys.iter()
        .find_map(|key| row.get(*key).and_then(Value::as_str))
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn row_usize(row: &Row, keys: &[&str]) -> Option<usize> {
    keys.iter().find_map(|key| row.get(*key)).and_then(|value| {
        // Negative i64 values are invalid counts/ranks and are intentionally
        // discarded by the fallible conversion.
        value
            .as_u64()
            .and_then(|value| usize::try_from(value).ok())
            .or_else(|| value.as_i64().and_then(|value| usize::try_from(value).ok()))
    })
}

fn row_f64(row: &Row, keys: &[&str]) -> Option<f64> {
    keys.iter()
        .find_map(|key| row.get(*key))
        .and_then(Value::as_f64)
}
