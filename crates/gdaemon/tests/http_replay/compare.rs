//! The comparison set and the first-difference rule (design-v2 sections 7.5.5, 7.6 and 7.8; H10).
//!
//! A comparison set is one document in case coordinates: `response`, then `db_delta[i].recorded`,
//! then `side_effects[i].recorded`. The recording and the observation are both put in this shape,
//! so one RFC 6901 pointer addresses either. `first_difference` mirrors `_first_difference` in
//! `tests/contracts/http_corpus.py`: objects compare keys in the order of the expected document
//! followed by the actual one, a missing key reports its own pointer, lists compare length and
//! then elements in order, and scalars compare by type and value.

use serde_json::Value;

use super::corpus::Case;
use super::normalize::{NormalizeError, Window};

/// Build the comparison document of one response, its `db_delta` values and its side effects.
/// Every value is redacted; masks and rules are applied by `normalized_document`.
pub fn comparison_document(response: &Value, db_delta: &[Value], side_effects: &[Value]) -> Value {
    unimplemented!("not wired: compare::comparison_document")
}

/// The first RFC 6901 pointer at which two comparison documents differ, or `None`.
pub fn first_difference(expected: &Value, actual: &Value) -> Option<String> {
    unimplemented!("not wired: compare::first_difference")
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
    unimplemented!("not wired: compare::normalized_document")
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
}
