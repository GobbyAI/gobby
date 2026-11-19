//! Field normalizers of the corpus v3 closed set (design-v2 section 7.6; H10, H16, H40).
//!
//! A normalization unit is one comparison document. Redaction runs first, then the case's masks,
//! then the rules. A rule's pointer may carry `*` segments, which match every element of an
//! array, so the leaves they match form one sequence in document order. `uuid_ordinal` numbers
//! UUIDs by first appearance in that order. `monotonic` masks integers and requires each
//! sequence to strictly increase. `timestamp` masks RFC 3339 values that lie inside the run
//! window and fails any value outside it. `bytes` and `none` leave the value unchanged.

use std::fmt;

use serde_json::Value;
use time::OffsetDateTime;

/// One closed-set normalizer. `set` is a per-table ordering declaration, not a field normalizer,
/// so it is refused here (see `db_delta`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Normalizer {
    Bytes,
    NoOp,
    UuidOrdinal,
    Monotonic,
    Timestamp,
}

/// The run window that a `timestamp` value must lie within (section 7.6).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Window {
    pub start: OffsetDateTime,
    pub end: OffsetDateTime,
}

/// A normalization that fails the case.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NormalizeError {
    /// The name is not in the closed set.
    UnknownName(String),
    /// `set` is declared per table, never per field.
    PerTableSet,
    /// A timestamp lies outside the run window.
    OutsideWindow { pointer: String },
    /// A monotonic sequence does not strictly increase at this pointer.
    NotMonotonic { pointer: String },
}

impl fmt::Display for NormalizeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            NormalizeError::UnknownName(name) => write!(f, "unknown normalizer {name:?}"),
            NormalizeError::PerTableSet => write!(f, "set is declared per table, not per field"),
            NormalizeError::OutsideWindow { pointer } => {
                write!(f, "timestamp at {pointer} lies outside the run window")
            }
            NormalizeError::NotMonotonic { pointer } => {
                write!(f, "monotonic sequence does not increase at {pointer}")
            }
        }
    }
}

impl std::error::Error for NormalizeError {}

/// Map one normalizer name of the closed set to its normalizer.
pub fn parse_name(name: &str) -> Result<Normalizer, NormalizeError> {
    unimplemented!("not wired: normalize::parse_name")
}

/// Redact the `SECRET_KEYS` values of `document` at any depth (the v2 rule, unchanged).
pub fn redact(document: &Value) -> Value {
    unimplemented!("not wired: normalize::redact")
}

/// Normalize one comparison document: redaction, then `mask` (RFC 6901 pointers replaced by
/// `@mask@`), then each `(pointer, normalizer)` rule in order.
pub fn normalize_document(
    document: &Value,
    mask: &Value,
    rules: &[(String, Normalizer)],
    window: &Window,
) -> Result<Value, NormalizeError> {
    unimplemented!("not wired: normalize::normalize_document")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use time::format_description::well_known::Rfc3339;

    const U1: &str = "11111111-1111-4111-8111-111111111111";
    const U2: &str = "22222222-2222-4222-8222-222222222222";

    fn at(text: &str) -> OffsetDateTime {
        OffsetDateTime::parse(text, &Rfc3339).expect("rfc3339 test timestamp")
    }

    fn one_hour_window() -> Window {
        Window {
            start: at("2023-11-14T22:00:00Z"),
            end: at("2023-11-14T23:00:00Z"),
        }
    }

    #[test]
    fn closed_set_names_parse_and_everything_else_is_refused() {
        assert_eq!(parse_name("uuid_ordinal"), Ok(Normalizer::UuidOrdinal));
        assert_eq!(parse_name("monotonic"), Ok(Normalizer::Monotonic));
        assert_eq!(parse_name("timestamp"), Ok(Normalizer::Timestamp));
        assert_eq!(parse_name("bytes"), Ok(Normalizer::Bytes));
        assert_eq!(parse_name("none"), Ok(Normalizer::NoOp));
        assert_eq!(parse_name("set"), Err(NormalizeError::PerTableSet));
        assert_eq!(
            parse_name("bogus"),
            Err(NormalizeError::UnknownName("bogus".to_owned()))
        );
    }

    #[test]
    fn uuid_ordinals_number_by_first_appearance_in_document_order() {
        let document = json!({
            "a": {"id": U1},
            "b": [U2, U1, U2]
        });
        let rules = [
            ("/a/id".to_owned(), Normalizer::UuidOrdinal),
            ("/b/*".to_owned(), Normalizer::UuidOrdinal),
        ];
        let normalized = normalize_document(&document, &json!([]), &rules, &one_hour_window())
            .expect("normalizes");
        assert_eq!(
            normalized,
            json!({"a": {"id": "@uuid:1@"}, "b": ["@uuid:2@", "@uuid:1@", "@uuid:2@"]})
        );
    }

    #[test]
    fn monotonic_sequences_mask_to_seq_and_must_strictly_increase() {
        let rules = [("/c/*/seq".to_owned(), Normalizer::Monotonic)];
        let rising = json!({"c": [{"seq": 1}, {"seq": 2}, {"seq": 7}]});
        let normalized = normalize_document(&rising, &json!([]), &rules, &one_hour_window())
            .expect("strictly increasing");
        assert_eq!(
            normalized,
            json!({"c": [{"seq": "@seq@"}, {"seq": "@seq@"}, {"seq": "@seq@"}]})
        );

        let falling = json!({"c": [{"seq": 2}, {"seq": 1}]});
        let error = normalize_document(&falling, &json!([]), &rules, &one_hour_window())
            .expect_err("reordered sequence");
        assert_eq!(
            error,
            NormalizeError::NotMonotonic {
                pointer: "/c/1/seq".to_owned()
            }
        );
    }

    #[test]
    fn timestamps_inside_the_window_mask_and_outside_fail() {
        let rules = [("/t".to_owned(), Normalizer::Timestamp)];
        let inside = json!({"t": "2023-11-14T22:30:00Z"});
        let normalized = normalize_document(&inside, &json!([]), &rules, &one_hour_window())
            .expect("inside the window");
        assert_eq!(normalized, json!({"t": "@ts@"}));

        let outside = json!({"t": "2023-11-15T00:00:00Z"});
        let error = normalize_document(&outside, &json!([]), &rules, &one_hour_window())
            .expect_err("outside the window");
        assert_eq!(
            error,
            NormalizeError::OutsideWindow {
                pointer: "/t".to_owned()
            }
        );
    }

    #[test]
    fn redaction_and_masks_run_before_the_rules() {
        let document = json!({"id": U1, "password": "hunter-two"});
        let rules = [("/id".to_owned(), Normalizer::UuidOrdinal)];
        let normalized = normalize_document(
            &document,
            &json!(["/id"]),
            &rules,
            &one_hour_window(),
        )
        .expect("normalizes");
        assert_eq!(normalized, json!({"id": "@mask@", "password": "@secret@"}));
    }

    #[test]
    fn redact_replaces_secret_keys_at_any_depth() {
        let document = json!({"outer": {"password": "p"}, "items": [{"dsn": "d"}]});
        assert_eq!(
            redact(&document),
            json!({"outer": {"password": "@secret@"}, "items": [{"dsn": "@secret@"}]})
        );
    }
}
