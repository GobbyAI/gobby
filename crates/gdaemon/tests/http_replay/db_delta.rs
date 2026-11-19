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
    use crate::http_replay::compare::{comparison_document, first_difference, normalized_document};
    use crate::http_replay::corpus::{Case, Kind};
    use crate::http_replay::normalize::{NormalizeError, Window};
    use serde_json::{Map, json};
    use time::OffsetDateTime;
    use time::format_description::well_known::Rfc3339;

    /// The three UUIDs of the parity goldens.
    const U1: &str = "11111111-1111-4111-8111-111111111111";
    const U2: &str = "22222222-2222-4222-8222-222222222222";
    const U3: &str = "33333333-3333-4333-8333-333333333333";

    /// The run window of the goldens: 2023-11-14 22:00 to 23:00 UTC.
    fn golden_window() -> Window {
        Window {
            start: OffsetDateTime::parse("2023-11-14T22:00:00Z", &Rfc3339).expect("start"),
            end: OffsetDateTime::parse("2023-11-14T23:00:00Z", &Rfc3339).expect("end"),
        }
    }

    /// One failure of an observed entry: a delta error or a normalizer error.
    #[derive(Debug, PartialEq)]
    enum Failure {
        Delta(DeltaError),
        Normalize(NormalizeError),
    }

    /// A case that declares one db_delta entry, with the entry's recorded value inside it.
    fn case_with(entry: Value) -> Case {
        Case {
            name: "parity-golden".to_owned(),
            schema_version: 3,
            kind: Kind::Http,
            family: "probe".to_owned(),
            backend: "up".to_owned(),
            ledger_key: Some("HTTP GET /api/probe/native".to_owned()),
            request: None,
            invocation: None,
            response: None,
            db_delta: vec![entry],
            side_effects: Vec::new(),
            normalizers: Map::new(),
            mask: json!([]),
            waiver: None,
            native_response: None,
        }
    }

    /// The comparison document of the recorded entry, as compare.rs builds it.
    fn expected(case: &Case) -> Value {
        comparison_document(&json!({}), &[case.db_delta[0]["recorded"].clone()], &[])
    }

    /// The comparison document of one observed entry, as compare.rs builds it.
    ///
    /// RED STAGE: this is today's pipeline. The raw delta is computed first, and compare.rs
    /// normalizes the changed values afterwards. The green stage replaces the body with
    /// `table_delta` over the same rows and entry; the tests below do not change.
    fn observed(case: &Case, before: &[Row], after: &[Row]) -> Result<Value, Failure> {
        let key: Vec<String> = case.db_delta[0]["key"]
            .as_array()
            .expect("key")
            .iter()
            .map(|name| name.as_str().expect("column").to_owned())
            .collect();
        let raw = delta(&key, &[], before, after).map_err(Failure::Delta)?;
        normalized_document(case, &json!({}), &[raw], &[], &golden_window())
            .map_err(Failure::Normalize)
    }

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

    // Parity goldens: each expected value is the output of `recorder_v3.table_delta` in the Python
    // worktree for the case named in its comment (scratch-wpE3a-parity/parity_golden_e3a.json).
    // Gap tests: 1 (changed only on normalized difference), 2 (changed key is the normalized key),
    // 3 (uuid ordinals numbered in Python order). The other tests pin behaviour both engines share.

    #[test]
    fn gap1_unchanged_normalized_column_is_not_reported() {
        // Golden: unchanged_normalized_column_is_not_reported.
        let case = case_with(json!({
            "table": "items", "key": ["id"], "columns": ["id", "updated_at", "value"],
            "normalizers": {"updated_at": "timestamp"}, "order": "set",
            "recorded": {"added": [], "removed": [], "changed": [
                {"key": {"id": 1}, "column": "value", "before": "a", "after": "b"}
            ]}
        }));
        let before = [row(json!({"id": 1, "updated_at": "2023-11-14T22:10:00+00:00", "value": "a"}))];
        let after = [row(json!({"id": 1, "updated_at": "2023-11-14T22:20:00+00:00", "value": "b"}))];
        let actual = observed(&case, &before, &after).expect("observed");
        assert_eq!(first_difference(&expected(&case), &actual), None);
    }

    #[test]
    fn gap2_changed_key_is_the_normalized_key() {
        // Golden: changed_key_is_the_normalized_key.
        let case = case_with(json!({
            "table": "owners", "key": ["owner_id"], "columns": ["owner_id", "revision"],
            "normalizers": {"owner_id": "uuid_ordinal"}, "order": "set",
            "recorded": {"added": [], "removed": [], "changed": [
                {"key": {"owner_id": "@uuid:1@"}, "column": "revision", "before": 1, "after": 2}
            ]}
        }));
        let before = [row(json!({"owner_id": U1, "revision": 1}))];
        let after = [row(json!({"owner_id": U1, "revision": 2}))];
        let actual = observed(&case, &before, &after).expect("observed");
        assert_eq!(first_difference(&expected(&case), &actual), None);
    }

    #[test]
    fn gap3_changed_pairs_number_all_befores_then_all_afters() {
        // Golden: changed_pairs_number_all_befores_then_all_afters.
        let case = case_with(json!({
            "table": "links", "key": ["id"], "columns": ["id", "owner"],
            "normalizers": {"owner": "uuid_ordinal"}, "order": "set",
            "recorded": {"added": [], "removed": [], "changed": [
                {"key": {"id": 1}, "column": "owner", "before": "@uuid:1@", "after": "@uuid:3@"},
                {"key": {"id": 2}, "column": "owner", "before": "@uuid:2@", "after": "@uuid:1@"}
            ]}
        }));
        let before = [row(json!({"id": 1, "owner": U1})), row(json!({"id": 2, "owner": U3}))];
        let after = [row(json!({"id": 1, "owner": U2})), row(json!({"id": 2, "owner": U1}))];
        let actual = observed(&case, &before, &after).expect("observed");
        assert_eq!(first_difference(&expected(&case), &actual), None);
    }

    #[test]
    fn one_counter_runs_over_added_removed_and_changed_images() {
        // Golden: one_counter_over_added_removed_and_changed (passes on both engines).
        let case = case_with(json!({
            "table": "links", "key": ["id"], "columns": ["id", "owner"],
            "normalizers": {"owner": "uuid_ordinal"}, "order": "set",
            "recorded": {
                "added": [{"id": 7, "owner": "@uuid:1@"}],
                "removed": [{"id": 8, "owner": "@uuid:2@"}],
                "changed": [{"key": {"id": 5}, "column": "owner", "before": "@uuid:3@", "after": "@uuid:1@"}]
            }
        }));
        let before = [row(json!({"id": 8, "owner": U1})), row(json!({"id": 5, "owner": U3}))];
        let after = [row(json!({"id": 5, "owner": U2})), row(json!({"id": 7, "owner": U2}))];
        let actual = observed(&case, &before, &after).expect("observed");
        assert_eq!(first_difference(&expected(&case), &actual), None);
    }

    #[test]
    fn uuid_numbering_follows_the_row_column_order() {
        // Golden: uuid_numbering_follows_the_row_column_order (passes on both engines).
        let case = case_with(json!({
            "table": "pairs", "key": ["id"], "columns": ["id", "zeta", "alpha"],
            "normalizers": {"zeta": "uuid_ordinal", "alpha": "uuid_ordinal"}, "order": "set",
            "recorded": {"added": [{"id": 1, "zeta": "@uuid:1@", "alpha": "@uuid:2@"}],
                         "removed": [], "changed": []}
        }));
        let after = [row(json!({"id": 1, "zeta": U2, "alpha": U1}))];
        let actual = observed(&case, &[], &after).expect("observed");
        assert_eq!(first_difference(&expected(&case), &actual), None);
    }

    #[test]
    fn an_out_of_window_timestamp_in_an_unchanged_column_is_an_error() {
        // Golden: out_of_window_unchanged_column_is_an_error (Python raises NormalizationError).
        let case = case_with(json!({
            "table": "items", "key": ["id"], "columns": ["id", "updated_at", "value"],
            "normalizers": {"updated_at": "timestamp"}, "order": "set",
            "recorded": {"added": [], "removed": [], "changed": []}
        }));
        let before = [row(json!({"id": 1, "updated_at": "2023-11-15T00:00:00+00:00", "value": "a"}))];
        let after = [row(json!({"id": 1, "updated_at": "2023-11-15T00:00:00+00:00", "value": "b"}))];
        let actual = observed(&case, &before, &after);
        assert!(matches!(actual, Err(Failure::Normalize(_))), "{actual:?}");
    }

    #[test]
    fn a_non_uuid_under_uuid_ordinal_is_an_error() {
        // Golden: non_uuid_under_uuid_ordinal_is_an_error (Python raises NormalizationError).
        let case = case_with(json!({
            "table": "links", "key": ["id"], "columns": ["id", "owner"],
            "normalizers": {"owner": "uuid_ordinal"}, "order": "set",
            "recorded": {"added": [], "removed": [], "changed": []}
        }));
        let after = [row(json!({"id": 1, "owner": "not-a-uuid"}))];
        let actual = observed(&case, &[], &after);
        assert!(matches!(actual, Err(Failure::Normalize(_))), "{actual:?}");
    }

    #[test]
    fn monotonic_under_set_order_is_an_error() {
        // Golden: monotonic_under_set_order_is_an_error (Python raises NormalizationError).
        let case = case_with(json!({
            "table": "seqs", "key": ["id"], "columns": ["id", "seq"],
            "normalizers": {"seq": "monotonic"}, "order": "set",
            "recorded": {"added": [], "removed": [], "changed": []}
        }));
        let after = [row(json!({"id": 1, "seq": 1}))];
        let actual = observed(&case, &[], &after);
        assert!(matches!(actual, Err(Failure::Normalize(_))), "{actual:?}");
    }

    #[test]
    fn a_timestamp_before_its_after_column_is_an_error() {
        // Golden: timestamp_after_violated_is_an_error (Python raises NormalizationError).
        let case = case_with(json!({
            "table": "stamps", "key": ["id"], "columns": ["id", "created_at", "updated_at"],
            "normalizers": {"updated_at": {"name": "timestamp", "after": "created_at"}},
            "order": "set",
            "recorded": {"added": [], "removed": [], "changed": []}
        }));
        let after = [row(json!({
            "id": 1, "created_at": "2023-11-14T23:00:00+00:00", "updated_at": "2023-11-14T22:30:00+00:00"
        }))];
        let actual = observed(&case, &[], &after);
        assert!(matches!(actual, Err(Failure::Normalize(_))), "{actual:?}");
    }

    #[test]
    fn a_timestamp_after_its_after_column_masks_to_ts() {
        // Golden: timestamp_after_ok_masks.
        let case = case_with(json!({
            "table": "stamps", "key": ["id"], "columns": ["id", "created_at", "updated_at"],
            "normalizers": {"updated_at": {"name": "timestamp", "after": "created_at"}},
            "order": "set",
            "recorded": {"added": [{"id": 1, "created_at": "2023-11-14T22:10:00+00:00",
                                    "updated_at": "@ts@"}],
                         "removed": [], "changed": []}
        }));
        let after = [row(json!({
            "id": 1, "created_at": "2023-11-14T22:10:00+00:00", "updated_at": "2023-11-14T22:20:00+00:00"
        }))];
        let actual = observed(&case, &[], &after).expect("observed");
        assert_eq!(first_difference(&expected(&case), &actual), None);
    }

    #[test]
    fn a_timestamp_only_pair_still_takes_ordinals_and_emits_no_changed_entry() {
        // Golden: timestamp_only_pair_still_consumes_ordinals.
        let case = case_with(json!({
            "table": "owners", "key": ["id"], "columns": ["id", "owner", "updated_at"],
            "normalizers": {"updated_at": "timestamp", "owner": "uuid_ordinal"}, "order": "set",
            "recorded": {"added": [], "removed": [], "changed": [
                {"key": {"id": 2}, "column": "owner", "before": "@uuid:2@", "after": "@uuid:3@"}
            ]}
        }));
        let before = [
            row(json!({"id": 1, "owner": U1, "updated_at": "2023-11-14T22:10:00+00:00"})),
            row(json!({"id": 2, "owner": U3, "updated_at": "2023-11-14T22:00:00+00:00"})),
        ];
        let after = [
            row(json!({"id": 1, "owner": U1, "updated_at": "2023-11-14T22:20:00+00:00"})),
            row(json!({"id": 2, "owner": U2, "updated_at": "2023-11-14T22:00:00+00:00"})),
        ];
        let actual = observed(&case, &before, &after).expect("observed");
        assert_eq!(first_difference(&expected(&case), &actual), None);
    }

    #[test]
    fn identity_order_uses_the_python_json_text() {
        // Golden: identity_order_uses_python_json_text. json.dumps escapes e-acute, so it sorts
        // before "z" in the key order.
        let case = case_with(json!({
            "table": "names", "key": ["name"], "columns": ["name", "v"],
            "normalizers": {}, "order": "set",
            "recorded": {"added": [{"name": "\u{e9}", "v": 2}, {"name": "z", "v": 1}],
                         "removed": [], "changed": []}
        }));
        let after = [row(json!({"name": "z", "v": 1})), row(json!({"name": "\u{e9}", "v": 2}))];
        let actual = observed(&case, &[], &after).expect("observed");
        assert_eq!(first_difference(&expected(&case), &actual), None);
    }

    #[test]
    fn a_duplicate_key_in_one_snapshot_is_an_error() {
        // Golden: duplicate_key_in_a_snapshot_is_an_error (Python raises RecorderError).
        let case = case_with(json!({
            "table": "items", "key": ["id"], "columns": ["id", "v"],
            "normalizers": {}, "order": "set",
            "recorded": {"added": [], "removed": [], "changed": []}
        }));
        let after = [row(json!({"id": 1, "v": 1})), row(json!({"id": 1, "v": 2}))];
        let actual = observed(&case, &[], &after);
        assert_eq!(
            actual,
            Err(Failure::Delta(DeltaError::DuplicateKey {
                key: "1".to_owned()
            }))
        );
    }

    #[test]
    fn an_empty_key_is_an_error() {
        let error = delta(&[], &columns(), &[], &[]).expect_err("empty key");
        assert_eq!(error, DeltaError::EmptyKey);
    }
}
