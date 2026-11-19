//! The comparison set and the first-difference rule (design-v2 sections 7.5.5, 7.6 and 7.8; H10).
//!
//! A comparison set is one document in case coordinates: `response`, then `db_delta[i].recorded`,
//! then `side_effects[i].recorded`. The recording and the observation are both put in this shape,
//! so one RFC 6901 pointer addresses either. `first_difference` mirrors `_first_difference` in
//! `tests/contracts/http_corpus.py`: objects compare keys in the order of the expected document
//! followed by the actual one, a missing key reports its own pointer, lists compare length and
//! then elements in order, and scalars compare by type and value.

use serde_json::{Value, json};

use super::corpus::Case;
use super::normalize::{
    NormalizeError, Window, escape_token, normalize_document, parse_spec, redact,
};

/// The normalizer declarations of an entry, from its `normalizers` object, in declaration order.
/// A `normalizers` value that is not an object is refused, not skipped.
fn normalizers_of(entry: &Value) -> Result<Vec<(&String, &Value)>, NormalizeError> {
    match entry.get("normalizers") {
        None => Ok(Vec::new()),
        Some(Value::Object(map)) => Ok(map.iter().collect()),
        Some(other) => Err(NormalizeError::BadSpec(other.to_string())),
    }
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

/// The comparison document of one case observation, normalized as the observer normalizes it.
///
/// `db_delta` values must already be the output of `db_delta::table_delta`, which normalizes each
/// entry as its own unit, so they are not normalized again here. `side_effects` values are the raw
/// captures of each entry, and the response is the raw response. Each entry's normalizers resolve
/// against the document: a case normalizer by its pointer, a side-effect normalizer on each capture
/// of its entry. Masks are applied before the rules here; Python applies them after normalization
/// (open item F2 in the report).
pub fn normalized_document(
    case: &Case,
    response: &Value,
    db_delta: &[Value],
    side_effects: &[Value],
    window: &Window,
) -> Result<Value, NormalizeError> {
    let document = comparison_document(response, db_delta, side_effects);
    let mut rules = Vec::new();
    for (pointer, spec) in &case.normalizers {
        rules.push((pointer.clone(), parse_spec(spec, false)?.normalizer));
    }
    for (index, entry) in case.side_effects.iter().enumerate() {
        for (pointer, spec) in normalizers_of(entry)? {
            rules.push((
                format!("/side_effects/{index}/recorded/*{pointer}"),
                parse_spec(spec, false)?.normalizer,
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
            crate::http_replay::db_delta::table_delta(
                &state_before,
                &state_after,
                &case.db_delta[0],
                &window,
            )
            .expect("config_state delta"),
            crate::http_replay::db_delta::table_delta(
                &store_before,
                &store_after,
                &case.db_delta[1],
                &window,
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
