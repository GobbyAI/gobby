//! Row deltas in the recorded shape (design-v2 sections 7.5.5 and 7.6; H16, H44).
//!
//! A snapshot of a declared table is taken before and after a case's request. `delta` keys both
//! snapshots by the declared key columns and returns `{added, removed, changed}`, the value a
//! `db_delta` entry records under `recorded`. Rows are sorted by key before anything else, so a
//! permuted snapshot gives the same value (the `set` order of H16). `changed` lists one entry per
//! changed column, sorted by key and then by column name.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Value, json};

/// One table row, keyed by column name.
pub type Row = Map<String, Value>;

/// Rows keyed by their identity: the JSON text of each key column, in key order. Ordering the
/// vectors lexicographically is the order Python's `sorted` gives the `json.dumps` tuples of
/// `recorder_v3._index`, so the text `"10"` sorts before `"9"`.
type Indexed<'a> = BTreeMap<Vec<String>, (Row, &'a Row)>;

/// Index `rows` by their key columns. A row without a key column, or two rows with one key, is an
/// error.
fn index_rows<'a>(key: &[String], rows: &'a [Row]) -> Result<Indexed<'a>, DeltaError> {
    let mut indexed = BTreeMap::new();
    for row in rows {
        let mut identity = Row::new();
        let mut text = Vec::with_capacity(key.len());
        for column in key {
            let value = row.get(column).ok_or_else(|| DeltaError::MissingColumn {
                column: column.clone(),
            })?;
            identity.insert(column.clone(), value.clone());
            text.push(serde_json::to_string(value).expect("a JSON value serializes"));
        }
        if indexed.insert(text.clone(), (identity, row)).is_some() {
            return Err(DeltaError::DuplicateKey {
                key: text.join(", "),
            });
        }
    }
    Ok(indexed)
}

/// `row` restricted to `columns`; an empty projection keeps every column.
fn project(row: &Row, columns: &[String]) -> Row {
    if columns.is_empty() {
        return row.clone();
    }
    columns
        .iter()
        .filter_map(|column| row.get(column).map(|value| (column.clone(), value.clone())))
        .collect()
}

/// A snapshot that cannot be keyed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DeltaError {
    /// The declared key is empty.
    EmptyKey,
    /// A row lacks one of the key columns.
    MissingColumn { column: String },
    /// Two rows share one key.
    DuplicateKey { key: String },
}

/// The recorded-shape delta between two snapshots of one table.
///
/// `columns` projects each row (an empty slice keeps every column of the row). `key` names the
/// columns that identify a row and must be non-empty.
pub fn delta(
    key: &[String],
    columns: &[String],
    before: &[Row],
    after: &[Row],
) -> Result<Value, DeltaError> {
    if key.is_empty() {
        return Err(DeltaError::EmptyKey);
    }
    let before = index_rows(key, before)?;
    let after = index_rows(key, after)?;
    let mut projection = columns.to_vec();
    projection.sort();
    projection.dedup();

    let mut added = Vec::new();
    for (identity, (_, row)) in &after {
        if !before.contains_key(identity) {
            added.push(Value::Object(project(row, &projection)));
        }
    }
    let mut removed = Vec::new();
    for (identity, (_, row)) in &before {
        if !after.contains_key(identity) {
            removed.push(Value::Object(project(row, &projection)));
        }
    }
    let mut changed = Vec::new();
    for (identity, (key_map, old)) in &before {
        let Some((_, new)) = after.get(identity) else {
            continue;
        };
        let names: Vec<String> = if projection.is_empty() {
            old.keys()
                .chain(new.keys())
                .cloned()
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect()
        } else {
            projection.clone()
        };
        for column in names {
            let previous = old.get(&column).cloned().unwrap_or(Value::Null);
            let current = new.get(&column).cloned().unwrap_or(Value::Null);
            if previous != current {
                changed.push(json!({
                    "key": Value::Object(key_map.clone()),
                    "column": column,
                    "before": previous,
                    "after": current,
                }));
            }
        }
    }
    Ok(json!({"added": added, "removed": removed, "changed": changed}))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn row(value: Value) -> Row {
        value.as_object().expect("row object").clone()
    }

    fn key() -> Vec<String> {
        vec!["id".to_owned()]
    }

    fn columns() -> Vec<String> {
        ["id", "revision", "value"].map(str::to_owned).to_vec()
    }

    #[test]
    fn one_changed_column_is_one_changed_entry() {
        let before = [row(json!({"id": 1, "revision": 1, "value": "a"}))];
        let after = [row(json!({"id": 1, "revision": 2, "value": "a"}))];
        assert_eq!(
            delta(&key(), &columns(), &before, &after).expect("delta"),
            json!({
                "added": [],
                "removed": [],
                "changed": [{"key": {"id": 1}, "column": "revision", "before": 1, "after": 2}]
            })
        );
    }

    #[test]
    fn added_and_removed_rows_carry_their_projected_columns() {
        let before = [row(json!({"id": 1, "revision": 1, "value": "a"}))];
        let after = [row(json!({"id": 2, "revision": 1, "value": "b"}))];
        let projected = ["id", "value"].map(str::to_owned).to_vec();
        assert_eq!(
            delta(&key(), &projected, &before, &after).expect("delta"),
            json!({
                "added": [{"id": 2, "value": "b"}],
                "removed": [{"id": 1, "value": "a"}],
                "changed": []
            })
        );
    }

    #[test]
    fn row_order_does_not_change_the_delta() {
        let before = [
            row(json!({"id": 1, "revision": 1, "value": "a"})),
            row(json!({"id": 2, "revision": 1, "value": "b"})),
        ];
        let after = [
            row(json!({"id": 2, "revision": 2, "value": "b"})),
            row(json!({"id": 1, "revision": 2, "value": "z"})),
        ];
        let mut reversed_before = before.clone();
        reversed_before.reverse();
        let mut reversed_after = after.clone();
        reversed_after.reverse();

        let forward = delta(&key(), &columns(), &before, &after).expect("forward");
        let permuted =
            delta(&key(), &columns(), &reversed_before, &reversed_after).expect("permuted");
        assert_eq!(forward, permuted);
        assert_eq!(
            forward["changed"],
            json!([
                {"key": {"id": 1}, "column": "revision", "before": 1, "after": 2},
                {"key": {"id": 1}, "column": "value", "before": "a", "after": "z"},
                {"key": {"id": 2}, "column": "revision", "before": 1, "after": 2}
            ]),
            "changed entries sort by key, then by column name"
        );
    }

    #[test]
    fn a_row_without_a_key_column_is_an_error() {
        let before = [row(json!({"revision": 1}))];
        let error = delta(&key(), &columns(), &before, &[]).expect_err("no key column");
        assert_eq!(
            error,
            DeltaError::MissingColumn {
                column: "id".to_owned()
            }
        );
    }

    #[test]
    fn delta_matches_the_python_table_delta_golden() {
        // The expected values are `table_delta` of recorder_v3 for these inputs, order "set",
        // with no normalizers (parity golden, one run of the Python module).
        let before = [
            row(json!({"id": 1, "revision": 1, "value": "gone"})),
            row(json!({"id": 9, "revision": 1, "value": "a"})),
            row(json!({"id": 10, "revision": 1, "value": "b"})),
        ];
        let after = [
            row(json!({"id": 9, "revision": 2, "value": "a"})),
            row(json!({"id": 10, "revision": 1, "value": "c"})),
            row(json!({"id": 11, "revision": 1, "value": "new"})),
        ];
        assert_eq!(
            delta(&key(), &[], &before, &after).expect("delta"),
            json!({
                "added": [{"id": 11, "revision": 1, "value": "new"}],
                "removed": [{"id": 1, "revision": 1, "value": "gone"}],
                "changed": [
                    {"key": {"id": 10}, "column": "value", "before": "b", "after": "c"},
                    {"key": {"id": 9}, "column": "revision", "before": 1, "after": 2}
                ]
            })
        );

        let before = [
            row(json!({"key": "b", "scope": "x", "n": 1})),
            row(json!({"key": "a", "scope": "y", "n": 1})),
        ];
        let after = [
            row(json!({"key": "b", "scope": "x", "n": 2})),
            row(json!({"key": "a", "scope": "y", "n": 1})),
        ];
        let two_column_key = ["key", "scope"].map(str::to_owned).to_vec();
        assert_eq!(
            delta(&two_column_key, &[], &before, &after).expect("delta"),
            json!({
                "added": [],
                "removed": [],
                "changed": [
                    {"key": {"key": "b", "scope": "x"}, "column": "n", "before": 1, "after": 2}
                ]
            })
        );
    }

    #[test]
    fn an_empty_key_is_an_error() {
        let error = delta(&[], &columns(), &[], &[]).expect_err("empty key");
        assert_eq!(error, DeltaError::EmptyKey);
    }
}
