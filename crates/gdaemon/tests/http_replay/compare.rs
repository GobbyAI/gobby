//! The comparison set and the first-difference rule (design-v2 sections 7.5.5, 7.6 and 7.8; H10).
//!
//! A comparison set is one document in case coordinates: `response`, then `db_delta[i].recorded`,
//! then `side_effects[i].recorded`. The recording and the observation are both put in this shape,
//! so one RFC 6901 pointer addresses either. `first_difference` mirrors `_first_difference` in
//! `tests/contracts/http_corpus.py`: objects compare keys in the order of the expected document
//! followed by the actual one, a missing key reports its own pointer, lists compare length and
//! then elements in order, and scalars compare by type and value.

use serde_json::{Map, Value, json};

use super::corpus::Case;
use super::normalize::{
    NormalizeError, Window, escape_token, normalize_document, parse_name, redact,
};

/// The normalizer names an entry declares, from its `normalizers` object.
fn normalizers_of(entry: &Value) -> Vec<(&String, &str)> {
    entry
        .get("normalizers")
        .and_then(Value::as_object)
        .into_iter()
        .flat_map(Map::iter)
        .filter_map(|(path, name)| name.as_str().map(|name| (path, name)))
        .collect()
}

/// Build the comparison document of one response, its `db_delta` values and its side effects.
/// Every value is redacted; masks and rules are applied by `normalized_document`.
pub fn comparison_document(response: &Value, db_delta: &[Value], side_effects: &[Value]) -> Value {
    let recorded = |values: &[Value]| -> Vec<Value> {
        values
            .iter()
            .map(|value| json!({"recorded": redact(value)}))
            .collect()
    };
    json!({
        "response": redact(response),
        "db_delta": recorded(db_delta),
        "side_effects": recorded(side_effects),
    })
}

/// The first RFC 6901 pointer at which two comparison documents differ, or `None`.
pub fn first_difference(expected: &Value, actual: &Value) -> Option<String> {
    first_difference_at(expected, actual, "")
}

/// The walk behind `first_difference`, at `pointer`.
fn first_difference_at(expected: &Value, actual: &Value, pointer: &str) -> Option<String> {
    match (expected, actual) {
        (Value::Object(left), Value::Object(right)) => {
            // Keys in the expected document's order, then keys only the actual one has.
            let keys = left
                .keys()
                .chain(right.keys().filter(|key| !left.contains_key(*key)));
            for key in keys {
                let child = format!("{pointer}/{}", escape_token(key));
                match (left.get(key), right.get(key)) {
                    (Some(left), Some(right)) => {
                        if let Some(found) = first_difference_at(left, right, &child) {
                            return Some(found);
                        }
                    }
                    _ => return Some(child),
                }
            }
            None
        }
        (Value::Array(left), Value::Array(right)) => {
            if left.len() != right.len() {
                return Some(pointer.to_owned());
            }
            left.iter()
                .zip(right)
                .enumerate()
                .find_map(|(index, (left, right))| {
                    first_difference_at(left, right, &format!("{pointer}/{index}"))
                })
        }
        _ if expected == actual => None,
        _ => Some(pointer.to_owned()),
    }
}

/// The comparison document of one case observation, with the case's masks and normalizers
/// applied. The normalizers of `db_delta` and `side_effects` entries are resolved against the
/// document, so a column normalizer reaches every row and changed value of its column, and a
/// side-effect normalizer reaches each capture in capture order.
pub fn normalized_document(
    case: &Case,
    response: &Value,
    db_delta: &[Value],
    side_effects: &[Value],
    window: &Window,
) -> Result<Value, NormalizeError> {
    let document = comparison_document(response, db_delta, side_effects);
    let mut rules = Vec::new();
    for (pointer, name) in &case.normalizers {
        let name = name.as_str().unwrap_or_default();
        rules.push((pointer.clone(), parse_name(name)?));
    }
    for (index, entry) in case.db_delta.iter().enumerate() {
        let base = format!("/db_delta/{index}/recorded");
        for (column, name) in normalizers_of(entry) {
            let normalizer = parse_name(name)?;
            let column = escape_token(column);
            rules.push((format!("{base}/added/*/{column}"), normalizer));
            rules.push((format!("{base}/removed/*/{column}"), normalizer));
            // A changed value is addressed by its entry, so the entries of this column expand.
            let changed = document
                .pointer(&format!("{base}/changed"))
                .and_then(Value::as_array);
            for (position, change) in changed.into_iter().flatten().enumerate() {
                if change["column"].as_str() == Some(column.as_str()) {
                    rules.push((format!("{base}/changed/{position}/before"), normalizer));
                    rules.push((format!("{base}/changed/{position}/after"), normalizer));
                }
            }
        }
    }
    for (index, entry) in case.side_effects.iter().enumerate() {
        for (pointer, name) in normalizers_of(entry) {
            rules.push((
                format!("/side_effects/{index}/recorded/*{pointer}"),
                parse_name(name)?,
            ));
        }
    }
    normalize_document(&document, &case.mask, &rules, window)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn comparison_document_uses_the_recorded_layout() {
        let document = comparison_document(
            &json!({"status": 200}),
            &[json!({"added": []})],
            &[json!([1])],
        );
        assert_eq!(
            document,
            json!({
                "response": {"status": 200},
                "db_delta": [{"recorded": {"added": []}}],
                "side_effects": [{"recorded": [1]}]
            })
        );
    }

    #[test]
    fn first_difference_reports_the_first_pointer_in_document_order() {
        assert_eq!(
            first_difference(&json!({"a": 1, "b": 2}), &json!({"a": 1, "b": 3})),
            Some("/b".to_owned())
        );
        assert_eq!(first_difference(&json!({"a": 1}), &json!({"a": 1})), None);
    }

    #[test]
    fn pointers_escape_slash_as_tilde_one_and_tilde_as_tilde_zero() {
        assert_eq!(
            first_difference(&json!({"a/b": {"c~d": 1}}), &json!({"a/b": {"c~d": 2}})),
            Some("/a~1b/c~0d".to_owned())
        );
    }

    #[test]
    fn a_missing_or_extra_key_reports_its_own_pointer() {
        assert_eq!(
            first_difference(&json!({"a": 1}), &json!({"a": 1, "x": 2})),
            Some("/x".to_owned())
        );
        assert_eq!(
            first_difference(&json!({"a": 1, "x": 2}), &json!({"a": 1})),
            Some("/x".to_owned())
        );
    }

    #[test]
    fn a_list_length_mismatch_reports_the_list_pointer() {
        assert_eq!(
            first_difference(&json!({"l": [1, 2]}), &json!({"l": [1]})),
            Some("/l".to_owned())
        );
    }

    #[test]
    fn an_element_mismatch_reports_the_element_pointer() {
        assert_eq!(
            first_difference(
                &json!({"l": [1, {"k": "v"}]}),
                &json!({"l": [1, {"k": "w"}]})
            ),
            Some("/l/1/k".to_owned())
        );
    }

    #[test]
    fn number_types_must_match() {
        assert_eq!(
            first_difference(&json!({"n": 1}), &json!({"n": 1.0})),
            Some("/n".to_owned())
        );
    }

    /// The column names of one db_delta entry field (`key` or `columns`).
    fn names_of(entry: &Value, field: &str) -> Vec<String> {
        entry[field]
            .as_array()
            .expect("entry names")
            .iter()
            .map(|name| name.as_str().expect("column name").to_owned())
            .collect()
    }

    /// The real v3 recording, end to end. The observed values are built from raw before and after
    /// rows that produce the recorded deltas, and the observed pg_notify payload is the sequence
    /// text "2". Only the observed values are normalized; the recording is compared as stored.
    #[test]
    fn the_real_config_values_recording_matches_observed_values_built_from_raw_rows() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/contracts/v3/config_values_patch.json");
        let value = gobby_test_support::corpus_v2::read_json(&path);
        let case = crate::http_replay::corpus::parse_case(&value).expect("the v3 case parses");
        let recorded = case.response.clone().expect("recorded response");
        let response = json!({"status": recorded["status"], "body": recorded["body"]});
        let window = crate::http_replay::normalize::Window {
            start: time::OffsetDateTime::UNIX_EPOCH,
            end: time::OffsetDateTime::UNIX_EPOCH,
        };

        let row = |value: Value| value.as_object().expect("row object").clone();
        let state_before = [row(json!({"id": true, "revision": 1}))];
        let state_after = [row(json!({"id": true, "revision": 2}))];
        let store_before = [row(json!({
            "key": "code_index.enabled", "value": "false",
            "source": "e2e-fixture", "is_secret": false, "revision": 1
        }))];
        let store_after = [row(json!({
            "key": "code_index.enabled", "value": "true",
            "source": "user", "is_secret": false, "revision": 2
        }))];
        let observed_db_delta = vec![
            crate::http_replay::db_delta::delta(
                &names_of(&case.db_delta[0], "key"),
                &names_of(&case.db_delta[0], "columns"),
                &state_before,
                &state_after,
            )
            .expect("config_state delta"),
            crate::http_replay::db_delta::delta(
                &names_of(&case.db_delta[1], "key"),
                &names_of(&case.db_delta[1], "columns"),
                &store_before,
                &store_after,
            )
            .expect("config_store delta"),
        ];
        // One side-effect entry, whose value is its list of captures.
        let observed_side_effects =
            vec![json!([{"channel": "gobby_config_changed", "payload": "2"}])];

        let expected = comparison_document(
            &response,
            &crate::http_replay::corpus::recorded_db_delta(&case).expect("recorded db_delta"),
            &crate::http_replay::corpus::recorded_side_effects(&case)
                .expect("recorded side effects"),
        );
        let actual = normalized_document(
            &case,
            &response,
            &observed_db_delta,
            &observed_side_effects,
            &window,
        )
        .expect("the observed values normalize");
        assert_eq!(first_difference(&expected, &actual), None);
    }
}
