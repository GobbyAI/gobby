//! The comparison set and the first-difference rule.
//!
//! A comparison set is one document in case coordinates: `response`, then `db_delta[i].recorded`,
//! then `side_effects[i].recorded`. The recording and the observation are both put in this shape,
//! so one RFC 6901 pointer addresses either. `first_difference` mirrors `_first_difference` in
//! `tests/contracts/http_corpus.py`: objects compare keys in the order of the expected document
//! followed by the actual one, a missing key reports its own pointer, lists compare length and
//! then elements in order, and scalars compare by type and value.

use gobby_test_support::corpus_v2;
use serde_json::{Value, json};

use super::corpus::{Case, Kind};
use super::normalize::{
    NormalizeError, Spec, Window, document_rules, escape_token, normalize_document,
    normalize_items, parse_spec, redact, sort_collections,
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

/// The side-effect rules of one entry, as pointers relative to each captured item (Python
/// `normalize_captures`, where `after` is refused).
fn capture_rules(entry: &Value) -> Result<Vec<(String, Spec)>, NormalizeError> {
    normalizers_of(entry)?
        .into_iter()
        .map(|(pointer, spec)| Ok((pointer.clone(), parse_spec(spec, false)?)))
        .collect()
}

/// The normalized comparison document of one case observation (Python `_normalize_response`,
/// `normalize_captures` and `_comparison`), with each unit normalized on its own counter.
///
/// The response is one unit: it is redacted, its `unordered` collections are sorted by their key
/// field, an HTTP response is masked before its rules (Python observes the masked answer), and then
/// the case's field rules run on `{"response": ...}`.
/// Each side-effect entry is one sequence unit of captures, normalized before any redaction. The
/// `db_delta` values must already be the output of `db_delta::table_delta`, which normalizes each
/// entry as its own unit. The assembled document is redacted, and the case's masks apply last, for
/// every kind.
pub fn normalized_document(
    case: &Case,
    response: &Value,
    db_delta: &[Value],
    side_effects: &[Value],
    window: &Window,
) -> Result<Value, NormalizeError> {
    let mut unit = json!({"response": redact(response)});
    let (collections, response_rules) = document_rules(&case.normalizers)?;
    sort_collections(&mut unit, &collections)?;
    if matches!(case.kind, Kind::Http) {
        corpus_v2::apply_masks(&mut unit, &case.mask);
    }
    let normalized = normalize_document(&unit, &json!([]), &response_rules, window)?;

    let mut recorded = Vec::new();
    for (index, captures) in side_effects.iter().enumerate() {
        let rules = match case.side_effects.get(index) {
            Some(entry) => capture_rules(entry)?,
            None => Vec::new(),
        };
        let items = captures
            .as_array()
            .ok_or_else(|| NormalizeError::NotAList {
                pointer: format!("side_effects[{index}]"),
            })?;
        recorded.push(Value::Array(normalize_items(
            items, &rules, true, "captures", window,
        )?));
    }

    let mut document = comparison_document(&normalized["response"], db_delta, &recorded);
    corpus_v2::apply_masks(&mut document, &case.mask);
    Ok(document)
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

    use super::super::corpus::Kind;
    use serde_json::Map;
    use time::OffsetDateTime;
    use time::format_description::well_known::Rfc3339;

    const U1: &str = "11111111-1111-4111-8111-111111111111";
    const U2: &str = "22222222-2222-4222-8222-222222222222";
    const UPPER: &str = "AABBCCDD-EEFF-4011-8122-334455667788";

    fn wide() -> Window {
        Window {
            start: OffsetDateTime::parse("2020-01-01T00:00:00Z", &Rfc3339).expect("start"),
            end: OffsetDateTime::parse("2030-01-01T00:00:00Z", &Rfc3339).expect("end"),
        }
    }

    /// A synthetic case: its kind, its mask, its document `normalizers` (an object) and its raw
    /// side-effect entries, each of which may carry its own `normalizers`.
    fn case_of(kind: Kind, mask: Value, normalizers: Value, side_effects: Vec<Value>) -> Case {
        Case {
            name: "synthetic".to_owned(),
            schema_version: 2,
            kind,
            family: "probe".to_owned(),
            backend: "up".to_owned(),
            ledger_key: None,
            request: None,
            invocation: None,
            response: None,
            db_delta: Vec::new(),
            side_effects,
            normalizers: normalizers.as_object().cloned().unwrap_or_else(Map::new),
            mask,
            waiver: None,
            native_response: None,
        }
    }

    #[test]
    fn a_response_and_a_capture_each_number_their_uuid_from_one() {
        // Golden (Python golden_f4.py, recorder_v3._normalize_response and normalize_captures): the
        // response and each capture entry are separate units with their own counters, so the
        // response UUID and a different capture UUID are both @uuid:1@.
        let case = case_of(
            Kind::Mcp,
            json!([]),
            json!({"/response/body/id": "uuid_ordinal"}),
            vec![json!({"normalizers": {"/owner": "uuid_ordinal"}})],
        );
        let normalized = normalized_document(
            &case,
            &json!({"body": {"id": U1}}),
            &[],
            &[json!([{"owner": U2}])],
            &wide(),
        );
        assert_eq!(
            normalized,
            Ok(json!({
                "response": {"body": {"id": "@uuid:1@"}},
                "db_delta": [],
                "side_effects": [{"recorded": [{"owner": "@uuid:1@"}]}]
            }))
        );
    }

    #[test]
    fn a_mask_applies_after_the_rules_on_a_non_http_case() {
        // Golden (Python _comparison, mcp): first takes ordinal 1 and is masked afterwards, so
        // second is @uuid:2@.
        let case = case_of(
            Kind::Mcp,
            json!(["/response/result/first"]),
            json!({
                "/response/result/first": "uuid_ordinal",
                "/response/result/second": "uuid_ordinal"
            }),
            Vec::new(),
        );
        let normalized = normalized_document(
            &case,
            &json!({"result": {"first": U1, "second": U2}}),
            &[],
            &[],
            &wide(),
        );
        assert_eq!(
            normalized,
            Ok(json!({
                "response": {"result": {"first": "@mask@", "second": "@uuid:2@"}},
                "db_delta": [],
                "side_effects": []
            }))
        );
    }

    #[test]
    fn an_http_answer_is_masked_before_its_rules_as_python_observes_it() {
        // Golden (Python recorder_v3._send, http_corpus.normalize_response): the mask runs before
        // the rules, so the rule refuses "@mask@" at /response/body/result/first.
        let case = case_of(
            Kind::Http,
            json!(["/response/body/result/first"]),
            json!({
                "/response/body/result/first": "uuid_ordinal",
                "/response/body/result/second": "uuid_ordinal"
            }),
            Vec::new(),
        );
        let response = json!({
            "status": 200,
            "headers": {},
            "body": {"result": {"first": U1, "second": U2}}
        });
        assert_eq!(
            normalized_document(&case, &response, &[], &[], &wide()),
            Err(NormalizeError::NotUuid {
                pointer: "/response/body/result/first".to_owned()
            })
        );
    }

    #[test]
    fn an_http_answer_is_sorted_before_a_positional_mask_applies() {
        // Golden (Python recorder_v3._normalize_response): the unordered collection is sorted by
        // its key before the mask on index 0, so the listed and reversed answers both mask alpha.
        let case = case_of(
            Kind::Http,
            json!(["/response/body/0/id"]),
            json!({"/response/body": {"name": "unordered", "key": "name"}}),
            Vec::new(),
        );
        let alpha = json!({"name": "alpha", "id": U1});
        let beta = json!({"name": "beta", "id": U2});
        let expected = json!({
            "response": {
                "status": 200,
                "headers": {},
                "body": [{"name": "alpha", "id": "@mask@"}, {"name": "beta", "id": U2}]
            },
            "db_delta": [],
            "side_effects": []
        });
        for (label, rows) in [
            ("as listed", json!([alpha.clone(), beta.clone()])),
            ("reversed", json!([beta, alpha])),
        ] {
            let response = json!({"status": 200, "headers": {}, "body": rows});
            assert_eq!(
                normalized_document(&case, &response, &[], &[], &wide()),
                Ok(expected.clone()),
                "the rows {label} normalize differently"
            );
        }
    }

    #[test]
    fn an_http_mask_on_a_container_refuses_a_none_rule_under_it() {
        // Golden (Python, http): the masked body is "@mask@", so /response/body/id matches nothing.
        let case = case_of(
            Kind::Http,
            json!(["/response/body"]),
            json!({"/response/body/id": "none"}),
            Vec::new(),
        );
        let response = json!({"status": 200, "headers": {}, "body": {"id": U1}});
        assert_eq!(
            normalized_document(&case, &response, &[], &[], &wide()),
            Err(NormalizeError::NoMatch {
                pointer: "/response/body/id".to_owned()
            })
        );
    }

    #[test]
    fn a_mask_on_a_container_keeps_a_none_rule_on_its_leaf_for_a_non_http_case() {
        // Golden (Python, mcp): the rule matches the leaf, then the container is masked.
        let case = case_of(
            Kind::Mcp,
            json!(["/response/body"]),
            json!({"/response/body/id": "none"}),
            Vec::new(),
        );
        let normalized =
            normalized_document(&case, &json!({"body": {"id": U1}}), &[], &[], &wide());
        assert_eq!(
            normalized,
            Ok(json!({
                "response": {"body": "@mask@"},
                "db_delta": [],
                "side_effects": []
            }))
        );
    }

    #[test]
    fn a_masked_wrong_typed_value_is_refused_as_python_refuses_it() {
        // Golden (Python, mcp): "uuid_ordinal needs a canonical UUID at /response/x".
        let case = case_of(
            Kind::Mcp,
            json!(["/response/x"]),
            json!({"/response/x": "uuid_ordinal"}),
            Vec::new(),
        );
        assert_eq!(
            normalized_document(&case, &json!({"x": "not-a-uuid"}), &[], &[], &wide()),
            Err(NormalizeError::NotUuid {
                pointer: "/response/x".to_owned()
            })
        );
    }

    #[test]
    fn a_capture_is_normalized_before_it_is_redacted() {
        // Golden (Python): normalize_captures runs in _run_case, redact_secrets in _comparison.
        let case = case_of(
            Kind::Mcp,
            json!([]),
            json!({}),
            vec![json!({"normalizers": {"/password": "uuid_ordinal"}})],
        );
        let normalized = normalized_document(
            &case,
            &json!({"ok": true}),
            &[],
            &[json!([{"password": U1}])],
            &wide(),
        );
        assert_eq!(
            normalized,
            Ok(json!({
                "response": {"ok": true},
                "db_delta": [],
                "side_effects": [{"recorded": [{"password": "@secret@"}]}]
            }))
        );
    }

    #[test]
    fn a_response_is_redacted_before_its_rules_as_python_redacts_it() {
        // Golden (Python _normalize_response): the secret key is redacted first, so the uuid rule
        // refuses "@secret@" at /response/password.
        let case = case_of(
            Kind::Mcp,
            json!([]),
            json!({"/response/password": "uuid_ordinal"}),
            Vec::new(),
        );
        assert_eq!(
            normalized_document(&case, &json!({"password": U1}), &[], &[], &wide()),
            Err(NormalizeError::NotUuid {
                pointer: "/response/password".to_owned()
            })
        );
    }

    #[test]
    fn a_capture_sequence_is_checked_across_items() {
        // Golden (Python normalize_captures): "4" then "5" become @seq@; "5" then "4" is refused.
        let case = case_of(
            Kind::Mcp,
            json!([]),
            json!({}),
            vec![json!({"normalizers": {"/p": "monotonic"}})],
        );
        let rising = normalized_document(
            &case,
            &json!({}),
            &[],
            &[json!([{"p": "4"}, {"p": "5"}])],
            &wide(),
        );
        assert_eq!(
            rising,
            Ok(json!({
                "response": {},
                "db_delta": [],
                "side_effects": [{"recorded": [{"p": "@seq@"}, {"p": "@seq@"}]}]
            }))
        );
        let falling = normalized_document(
            &case,
            &json!({}),
            &[],
            &[json!([{"p": "5"}, {"p": "4"}])],
            &wide(),
        )
        .expect_err("a falling sequence");
        assert!(
            falling.to_string().contains("strictly increasing"),
            "{falling}"
        );
    }

    #[test]
    fn a_scalar_capture_item_is_refused() {
        // Golden (Python): "captures[0] must be an object".
        let case = case_of(
            Kind::Mcp,
            json!([]),
            json!({}),
            vec![json!({"normalizers": {"/payload": "none"}})],
        );
        let error = normalized_document(&case, &json!({}), &[], &[json!([5])], &wide())
            .expect_err("a scalar item cannot be normalized");
        assert!(
            error.to_string().contains("captures[0] must be an object"),
            "{error}"
        );
    }

    #[test]
    fn a_capture_rule_that_matches_nothing_in_one_item_fails_the_entry() {
        // Golden (Python): "normalizer pointer captures[1]/payload matches nothing".
        let case = case_of(
            Kind::Mcp,
            json!([]),
            json!({}),
            vec![json!({"normalizers": {"/payload": "monotonic"}})],
        );
        let error = normalized_document(
            &case,
            &json!({}),
            &[],
            &[json!([{"payload": "4"}, {"other": 1}])],
            &wide(),
        )
        .expect_err("item 1 has no payload");
        assert!(
            error
                .to_string()
                .contains("captures[1]/payload matches nothing"),
            "{error}"
        );
    }

    #[test]
    fn an_empty_capture_list_never_raises() {
        // Golden (Python): normalize_captures([], rules) returns [].
        let case = case_of(
            Kind::Mcp,
            json!([]),
            json!({}),
            vec![json!({"normalizers": {"/payload": "monotonic"}})],
        );
        assert_eq!(
            normalized_document(&case, &json!({}), &[], &[json!([])], &wide()),
            Ok(json!({
                "response": {},
                "db_delta": [],
                "side_effects": [{"recorded": []}]
            }))
        );
    }

    #[test]
    fn an_uppercase_uuid_in_a_capture_is_refused_at_its_capture_pointer() {
        // Golden (Python): "uuid_ordinal needs a canonical UUID at captures[0]/owner".
        let case = case_of(
            Kind::Mcp,
            json!([]),
            json!({}),
            vec![json!({"normalizers": {"/owner": "uuid_ordinal"}})],
        );
        let error = normalized_document(
            &case,
            &json!({}),
            &[],
            &[json!([{"owner": UPPER}])],
            &wide(),
        )
        .expect_err("an uppercase UUID");
        assert!(error.to_string().contains("captures[0]/owner"), "{error}");
    }

    #[test]
    fn a_monotonic_response_rule_is_refused() {
        // Golden (Python normalize_document): "monotonic needs a sequence at /response/x".
        let case = case_of(
            Kind::Mcp,
            json!([]),
            json!({"/response/x": "monotonic"}),
            Vec::new(),
        );
        assert_eq!(
            normalized_document(&case, &json!({"x": 3}), &[], &[], &wide()),
            Err(NormalizeError::MonotonicInDocument {
                pointer: "/response/x".to_owned()
            })
        );
    }
}
