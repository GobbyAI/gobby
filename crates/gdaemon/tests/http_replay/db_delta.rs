//! Row deltas in the recorded shape (design-v2 sections 7.5.5 and 7.6; H16, H44).
//!
//! A snapshot of a declared table is taken before and after a case's request. `delta` keys both
//! snapshots by the declared key columns and returns `{added, removed, changed}`, the value a
//! `db_delta` entry records under `recorded`. Rows are sorted by key before anything else, so a
//! permuted snapshot gives the same value (the `set` order of H16). `changed` lists one entry per
//! changed column, sorted by key and then by column name.

use serde_json::{Map, Value};

/// One table row, keyed by column name.
pub type Row = Map<String, Value>;

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
    unimplemented!("not wired: db_delta::delta")
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
        let permuted = delta(&key(), &columns(), &reversed_before, &reversed_after).expect("permuted");
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
    fn an_empty_key_is_an_error() {
        let error = delta(&[], &columns(), &[], &[]).expect_err("empty key");
        assert_eq!(error, DeltaError::EmptyKey);
    }
}
