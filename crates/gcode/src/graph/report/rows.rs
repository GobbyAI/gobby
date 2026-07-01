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
            let count = row_usize(&row, &["count"])?;
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
    for key in keys {
        let Some(value) = row.get(*key).and_then(Value::as_str) else {
            continue;
        };
        if !value.is_empty() {
            return Some(value.to_string());
        }
    }
    None
}

fn row_usize(row: &Row, keys: &[&str]) -> Option<usize> {
    for key in keys {
        let Some(value) = row.get(*key) else {
            continue;
        };
        if let Some(value) = value.as_u64().and_then(|value| usize::try_from(value).ok()) {
            return Some(value);
        }
        if let Some(value) = value.as_i64() {
            if value < 0 {
                eprintln!("Warning: ignoring negative graph report value {key}={value}");
                return None;
            }
            if let Ok(value) = usize::try_from(value) {
                return Some(value);
            }
        }
        if let Some(value) = value.as_f64().and_then(|value| {
            (value >= 0.0 && value.fract() == 0.0)
                .then_some(value as u64)
                .and_then(|value| usize::try_from(value).ok())
        }) {
            return Some(value);
        }
    }
    None
}

fn row_f64(row: &Row, keys: &[&str]) -> Option<f64> {
    keys.iter()
        .find_map(|key| row.get(*key))
        .and_then(Value::as_f64)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn row_string_skips_empty_candidate_keys() {
        let mut row = Row::new();
        row.insert("first".to_string(), Value::String(String::new()));
        row.insert("second".to_string(), Value::String("value".to_string()));

        assert_eq!(
            row_string(&row, &["first", "second"]).as_deref(),
            Some("value")
        );
    }

    #[test]
    fn row_usize_skips_invalid_candidate_keys() {
        let mut row = Row::new();
        row.insert(
            "first".to_string(),
            Value::String("not-a-number".to_string()),
        );
        row.insert("second".to_string(), Value::from(7));

        assert_eq!(row_usize(&row, &["first", "second"]), Some(7));
    }

    #[test]
    fn rows_to_named_counts_skips_missing_counts() {
        let mut missing = Row::new();
        missing.insert("name".to_string(), Value::String("missing".to_string()));
        let mut present = Row::new();
        present.insert("name".to_string(), Value::String("present".to_string()));
        present.insert("count".to_string(), Value::from(3));

        let counts = rows_to_named_counts(vec![missing, present]);

        assert_eq!(counts.get("missing"), None);
        assert_eq!(counts.get("present"), Some(&3));
    }

    #[test]
    fn row_usize_rejects_negative_values() {
        let mut row = Row::new();
        row.insert("count".to_string(), Value::from(-1));

        assert_eq!(row_usize(&row, &["count"]), None);
    }
}
