//! Row deltas in the recorded shape.
//!
//! `table_delta` ports `recorder_v3.table_delta`. Both snapshots of a declared table are keyed by
//! the entry's key columns, and a row is added, removed or in both. A row in both is a pair only
//! when its raw before and after rows differ. The images of the entry (the added rows, the removed
//! rows, then the before and after image of each pair) are normalized as one unit by
//! `normalize_images`, so one `uuid_ordinal` counter covers them in that order. A changed column is
//! reported only when its normalized before and after values differ, and a changed entry's key is
//! the normalized key. Rows are sorted by identity, so a permuted snapshot gives the same value.

use std::collections::BTreeMap;

use serde_json::{Map, Value, json};

use super::normalize::{NormalizeError, Spec, Window, normalize_images, parse_order, parse_spec};

/// One table row, keyed by column name. Its columns keep the order the snapshot read them in,
/// which the `uuid_ordinal` numbering follows.
pub type Row = Map<String, Value>;

/// A row's identity: the Python JSON text of each key column, in key order (see `identity_text`).
type Identity = Vec<String>;

/// A snapshot or entry that cannot be read as the entry declares.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DeltaError {
    /// The declared key is empty.
    EmptyKey,
    /// A row lacks one of the key columns.
    MissingColumn { column: String },
    /// Two rows share one key.
    DuplicateKey { key: String },
    /// The entry lacks a field the delta needs, or the field has the wrong shape.
    BadEntry { field: &'static str },
    /// A normalizer fails the entry.
    Normalize(NormalizeError),
}

/// The entry's key columns (`entry["key"]`).
fn key_of(entry: &Value) -> Result<Vec<String>, DeltaError> {
    let names = entry
        .get("key")
        .and_then(Value::as_array)
        .ok_or(DeltaError::BadEntry { field: "key" })?;
    names
        .iter()
        .map(|name| {
            name.as_str()
                .map(str::to_owned)
                .ok_or(DeltaError::BadEntry { field: "key" })
        })
        .collect()
}

/// The entry's column normalizers, in declaration order (`entry.get("normalizers", {})`).
fn rules_of(entry: &Value) -> Result<Vec<(String, Spec)>, DeltaError> {
    match entry.get("normalizers") {
        None => Ok(Vec::new()),
        Some(Value::Object(map)) => map
            .iter()
            .map(|(column, spec)| {
                parse_spec(spec, true)
                    .map(|spec| (column.clone(), spec))
                    .map_err(DeltaError::Normalize)
            })
            .collect(),
        Some(_) => Err(DeltaError::BadEntry {
            field: "normalizers",
        }),
    }
}

/// Index rows by identity. A row without a key column, or two rows with one identity, is an error.
fn index_rows<'a>(
    key: &[String],
    rows: &'a [Row],
) -> Result<BTreeMap<Identity, &'a Row>, DeltaError> {
    let mut indexed = BTreeMap::new();
    for row in rows {
        let mut identity = Identity::with_capacity(key.len());
        for column in key {
            let value = row.get(column).ok_or_else(|| DeltaError::MissingColumn {
                column: column.clone(),
            })?;
            identity.push(identity_text(value));
        }
        let text = identity.join(", ");
        if indexed.insert(identity, row).is_some() {
            return Err(DeltaError::DuplicateKey { key: text });
        }
    }
    Ok(indexed)
}

/// The text `json.dumps(value, sort_keys=True)` gives in `recorder_v3._index`. Identities are
/// ordered by this text, so it follows Python: object keys are sorted, and every character outside
/// printable ASCII is escaped (`ensure_ascii`), which orders `"é"` before `"z"`.
fn identity_text(value: &Value) -> String {
    match value {
        Value::Null => "null".to_owned(),
        Value::Bool(true) => "true".to_owned(),
        Value::Bool(false) => "false".to_owned(),
        Value::Number(number) => number.to_string(),
        Value::String(text) => ascii_json_string(text),
        Value::Array(items) => {
            let members: Vec<String> = items.iter().map(identity_text).collect();
            format!("[{}]", members.join(", "))
        }
        Value::Object(fields) => {
            let mut keys: Vec<&String> = fields.keys().collect();
            keys.sort();
            let members: Vec<String> = keys
                .into_iter()
                .map(|key| {
                    format!(
                        "{}: {}",
                        ascii_json_string(key),
                        identity_text(&fields[key])
                    )
                })
                .collect();
            format!("{{{}}}", members.join(", "))
        }
    }
}

/// One JSON string as `json.dumps` writes it with `ensure_ascii`.
fn ascii_json_string(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 2);
    out.push('"');
    for ch in text.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(ch),
            _ => {
                let mut units = [0u16; 2];
                for unit in ch.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
    out
}

/// The key of a changed entry: the normalized values of the key columns of `fields`.
fn key_map(fields: &Map<String, Value>, key: &[String]) -> Value {
    Value::Object(
        key.iter()
            .filter_map(|column| {
                fields
                    .get(column)
                    .map(|value| (column.clone(), value.clone()))
            })
            .collect(),
    )
}

/// The normalized delta of one db_delta entry between two snapshots of its table
/// (`recorder_v3.table_delta`).
///
/// `before` and `after` are the table's rows as the snapshot read them, each with the entry's
/// columns in the entry's order. `entry` is the declared entry: its `key`, its `order` and its
/// `normalizers`. The result is the recorded shape `{added, removed, changed}`, normalized.
pub fn table_delta(
    before: &[Row],
    after: &[Row],
    entry: &Value,
    window: &Window,
) -> Result<Value, DeltaError> {
    let key = key_of(entry)?;
    if key.is_empty() {
        return Err(DeltaError::EmptyKey);
    }
    let order = parse_order(
        entry
            .get("order")
            .and_then(Value::as_str)
            .ok_or(DeltaError::BadEntry { field: "order" })?,
    )
    .map_err(DeltaError::Normalize)?;
    let rules = rules_of(entry)?;
    let before_by = index_rows(&key, before)?;
    let after_by = index_rows(&key, after)?;

    let mut added: Vec<Value> = Vec::new();
    for (identity, row) in &after_by {
        if !before_by.contains_key(identity) {
            added.push(Value::Object(Row::clone(row)));
        }
    }
    let mut removed: Vec<Value> = Vec::new();
    for (identity, row) in &before_by {
        if !after_by.contains_key(identity) {
            removed.push(Value::Object(Row::clone(row)));
        }
    }
    let mut pairs: Vec<(&Row, &Row)> = Vec::new();
    for (identity, old) in &before_by {
        if let Some(new) = after_by.get(identity).copied().filter(|new| new != old) {
            pairs.push((*old, new));
        }
    }

    let mut images: Vec<Value> = Vec::with_capacity(added.len() + removed.len() + 2 * pairs.len());
    images.extend(added.iter().cloned());
    images.extend(removed.iter().cloned());
    images.extend(pairs.iter().map(|(old, _)| Value::Object(Row::clone(old))));
    images.extend(pairs.iter().map(|(_, new)| Value::Object(Row::clone(new))));
    let normalized =
        normalize_images(&images, &rules, order, window).map_err(DeltaError::Normalize)?;

    let (added_out, rest) = normalized.split_at(added.len());
    let (removed_out, rest) = rest.split_at(removed.len());
    let (old_out, new_out) = rest.split_at(pairs.len());
    let mut changed = Vec::new();
    for (old_row, new_row) in old_out.iter().zip(new_out) {
        // Every image is an object: each one was built from a row above.
        let (Some(old_fields), Some(new_fields)) = (old_row.as_object(), new_row.as_object())
        else {
            continue;
        };
        let mut columns: Vec<&String> = old_fields.keys().collect();
        columns.sort();
        for column in columns {
            let before_value = old_fields.get(column);
            let after_value = new_fields.get(column);
            if before_value != after_value {
                changed.push(json!({
                    "key": key_map(old_fields, &key),
                    "column": column,
                    "before": before_value.cloned().unwrap_or(Value::Null),
                    "after": after_value.cloned().unwrap_or(Value::Null),
                }));
            }
        }
    }
    Ok(json!({"added": added_out, "removed": removed_out, "changed": changed}))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::http_replay::compare::{comparison_document, first_difference, normalized_document};
    use crate::http_replay::corpus::{Case, Kind};
    use crate::http_replay::normalize::NormalizeError;
    use serde_json::{Map, json};
    use time::OffsetDateTime;
    use time::format_description::well_known::Rfc3339;

    /// The three UUIDs of the parity goldens.
    const U1: &str = "11111111-1111-4111-8111-111111111111";
    const U2: &str = "22222222-2222-4222-8222-222222222222";
    const U3: &str = "33333333-3333-4333-8333-333333333333";

    fn row(value: Value) -> Row {
        value.as_object().expect("row object").clone()
    }

    /// The run window of the goldens: 2023-11-14 22:00 to 23:00 UTC.
    fn golden_window() -> Window {
        Window {
            start: OffsetDateTime::parse("2023-11-14T22:00:00Z", &Rfc3339).expect("start"),
            end: OffsetDateTime::parse("2023-11-14T23:00:00Z", &Rfc3339).expect("end"),
        }
    }

    /// A declared entry without a recorded value.
    fn entry(key: &[&str], order: &str, normalizers: Value) -> Value {
        json!({"table": "t", "key": key, "order": order, "normalizers": normalizers})
    }

    #[test]
    fn one_changed_column_is_one_changed_entry() {
        let before = [row(json!({"id": 1, "revision": 1, "value": "a"}))];
        let after = [row(json!({"id": 1, "revision": 2, "value": "a"}))];
        assert_eq!(
            table_delta(
                &before,
                &after,
                &entry(&["id"], "set", json!({})),
                &golden_window()
            )
            .expect("delta"),
            json!({
                "added": [],
                "removed": [],
                "changed": [{"key": {"id": 1}, "column": "revision", "before": 1, "after": 2}]
            })
        );
    }

    #[test]
    fn added_and_removed_rows_are_the_rows_as_read() {
        let before = [row(json!({"id": 1, "revision": 1, "value": "a"}))];
        let after = [row(json!({"id": 2, "revision": 1, "value": "b"}))];
        assert_eq!(
            table_delta(
                &before,
                &after,
                &entry(&["id"], "set", json!({})),
                &golden_window()
            )
            .expect("delta"),
            json!({
                "added": [{"id": 2, "revision": 1, "value": "b"}],
                "removed": [{"id": 1, "revision": 1, "value": "a"}],
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
        let declared = entry(&["id"], "set", json!({}));

        let forward = table_delta(&before, &after, &declared, &golden_window()).expect("forward");
        let permuted = table_delta(
            &reversed_before,
            &reversed_after,
            &declared,
            &golden_window(),
        )
        .expect("permuted");
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
        let error = table_delta(
            &before,
            &[],
            &entry(&["id"], "set", json!({})),
            &golden_window(),
        )
        .expect_err("no key column");
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
            table_delta(
                &before,
                &after,
                &entry(&["id"], "set", json!({})),
                &golden_window()
            )
            .expect("delta"),
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
        assert_eq!(
            table_delta(
                &before,
                &after,
                &entry(&["key", "scope"], "set", json!({})),
                &golden_window()
            )
            .expect("delta"),
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
        let error = table_delta(&[], &[], &entry(&[], "set", json!({})), &golden_window())
            .expect_err("empty key");
        assert_eq!(error, DeltaError::EmptyKey);
    }

    // Parity goldens: each expected value is the output of `recorder_v3.table_delta` in the Python
    // worktree for the case named in its comment (scratch-wpE3a-parity/parity_golden_e3a.json).
    // Gap tests: 1 (changed only on normalized difference), 2 (changed key is the normalized key),
    // 3 (uuid ordinals numbered in Python order). The other tests pin behaviour both engines share.

    /// Failures of one observed entry: a delta error or a normalizer error, as one type.
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

    /// The comparison document of one observed entry, as compare.rs builds it: the entry's
    /// normalized delta, placed in the case's comparison document.
    fn observed(case: &Case, before: &[Row], after: &[Row]) -> Result<Value, Failure> {
        let value =
            table_delta(before, after, &case.db_delta[0], &golden_window()).map_err(|error| {
                match error {
                    DeltaError::Normalize(error) => Failure::Normalize(error),
                    other => Failure::Delta(other),
                }
            })?;
        normalized_document(case, &json!({}), &[value], &[], &golden_window())
            .map_err(Failure::Normalize)
    }

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
        let before = [row(
            json!({"id": 1, "updated_at": "2023-11-14T22:10:00+00:00", "value": "a"}),
        )];
        let after = [row(
            json!({"id": 1, "updated_at": "2023-11-14T22:20:00+00:00", "value": "b"}),
        )];
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
        let before = [
            row(json!({"id": 1, "owner": U1})),
            row(json!({"id": 2, "owner": U3})),
        ];
        let after = [
            row(json!({"id": 1, "owner": U2})),
            row(json!({"id": 2, "owner": U1})),
        ];
        let actual = observed(&case, &before, &after).expect("observed");
        assert_eq!(first_difference(&expected(&case), &actual), None);
    }

    #[test]
    fn one_counter_runs_over_added_removed_and_changed_images() {
        // Golden: one_counter_over_added_removed_and_changed.
        let case = case_with(json!({
            "table": "links", "key": ["id"], "columns": ["id", "owner"],
            "normalizers": {"owner": "uuid_ordinal"}, "order": "set",
            "recorded": {
                "added": [{"id": 7, "owner": "@uuid:1@"}],
                "removed": [{"id": 8, "owner": "@uuid:2@"}],
                "changed": [{"key": {"id": 5}, "column": "owner", "before": "@uuid:3@", "after": "@uuid:1@"}]
            }
        }));
        let before = [
            row(json!({"id": 8, "owner": U1})),
            row(json!({"id": 5, "owner": U3})),
        ];
        let after = [
            row(json!({"id": 5, "owner": U2})),
            row(json!({"id": 7, "owner": U2})),
        ];
        let actual = observed(&case, &before, &after).expect("observed");
        assert_eq!(first_difference(&expected(&case), &actual), None);
    }

    #[test]
    fn uuid_numbering_follows_the_row_column_order() {
        // Golden: uuid_numbering_follows_the_row_column_order.
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
        let before = [row(
            json!({"id": 1, "updated_at": "2023-11-15T00:00:00+00:00", "value": "a"}),
        )];
        let after = [row(
            json!({"id": 1, "updated_at": "2023-11-15T00:00:00+00:00", "value": "b"}),
        )];
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
        let after = [
            row(json!({"name": "z", "v": 1})),
            row(json!({"name": "\u{e9}", "v": 2})),
        ];
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

    // `as_recorded` pins: the images are monotonic-checked in the order `table_delta` gives them,
    // which is identity order for added rows and before-then-after for each pair. Each golden is
    // the Python result of the same entry and rows (`recorder_v3.table_delta`, parity golden).

    /// An `as_recorded` entry with a monotonic `seq` and a `v` column.
    fn sequence_entry() -> Value {
        json!({"table": "seqs", "key": ["id"], "columns": ["id", "seq", "v"],
               "normalizers": {"seq": "monotonic"}, "order": "as_recorded"})
    }

    #[test]
    fn as_recorded_added_rows_are_checked_in_identity_order() {
        // Golden: as_recorded_added_rows_follow_identity_order_for_monotonic_error. Identity "10"
        // sorts before "9", so the sequence is 2 then 1: Python raises.
        let after = [
            row(json!({"id": 9, "seq": 1})),
            row(json!({"id": 10, "seq": 2})),
        ];
        let entry = json!({"table": "seqs", "key": ["id"], "columns": ["id", "seq"],
                           "normalizers": {"seq": "monotonic"}, "order": "as_recorded"});
        let error = table_delta(&[], &after, &entry, &golden_window())
            .expect_err("not increasing in identity order");
        assert_eq!(
            error,
            DeltaError::Normalize(NormalizeError::NotMonotonic {
                pointer: "rows[1]/seq".to_owned()
            })
        );
    }

    #[test]
    fn as_recorded_sequence_in_identity_order_masks() {
        // Golden: as_recorded_added_rows_in_identity_order_mask.
        let after = [
            row(json!({"id": 10, "seq": 1})),
            row(json!({"id": 9, "seq": 2})),
        ];
        let entry = json!({"table": "seqs", "key": ["id"], "columns": ["id", "seq"],
                           "normalizers": {"seq": "monotonic"}, "order": "as_recorded"});
        assert_eq!(
            table_delta(&[], &after, &entry, &golden_window()).expect("masks"),
            json!({
                "added": [{"id": 10, "seq": "@seq@"}, {"id": 9, "seq": "@seq@"}],
                "removed": [],
                "changed": []
            })
        );
    }

    #[test]
    fn as_recorded_equal_sequence_across_a_pair_is_an_error() {
        // Golden: as_recorded_equal_seq_across_a_pair_is_an_error. The before and after images
        // are one unit, so an equal seq on both sides is not strictly increasing.
        let before = [row(json!({"id": 1, "seq": 5, "v": "a"}))];
        let after = [row(json!({"id": 1, "seq": 5, "v": "b"}))];
        let error = table_delta(&before, &after, &sequence_entry(), &golden_window())
            .expect_err("equal sequence");
        assert_eq!(
            error,
            DeltaError::Normalize(NormalizeError::NotMonotonic {
                pointer: "rows[1]/seq".to_owned()
            })
        );
    }

    #[test]
    fn as_recorded_increasing_sequence_across_a_pair_masks_and_reports_only_v() {
        // Golden: as_recorded_increasing_seq_across_a_pair_masks.
        let before = [row(json!({"id": 1, "seq": 5, "v": "a"}))];
        let after = [row(json!({"id": 1, "seq": 6, "v": "b"}))];
        assert_eq!(
            table_delta(&before, &after, &sequence_entry(), &golden_window()).expect("masks"),
            json!({
                "added": [],
                "removed": [],
                "changed": [{"key": {"id": 1}, "column": "v", "before": "a", "after": "b"}]
            })
        );
    }
}
