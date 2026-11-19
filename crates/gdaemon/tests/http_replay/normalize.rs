//! Field normalizers of the corpus v3 closed set (design-v2 section 7.6; H10, H16, H40).
//!
//! A normalization unit is one comparison document. Redaction runs first, then the case's masks,
//! then the rules. A rule's pointer may carry `*` segments, which match every element of an
//! array, so the leaves they match form one sequence in document order. `uuid_ordinal` numbers
//! UUIDs by first appearance in that order. `monotonic` masks integers and decimal text (`-?[0-9]+`,
//! as `pg_notify` payloads arrive) and requires each sequence to strictly increase; any other
//! value is an error, as in `_sequence_value` of `normalizers_v3.py`. `timestamp` masks RFC 3339 values that lie inside the run
//! window and fails any value outside it. `bytes` and `none` leave the value unchanged.

use std::fmt;

use std::collections::BTreeMap;

use gobby_test_support::corpus_v2;
use serde_json::{Value, json};
use time::OffsetDateTime;
use time::format_description::well_known::Rfc3339;

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
    /// A monotonic value is neither an integer nor decimal text at this pointer.
    NotInteger { pointer: String },
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
            NormalizeError::NotInteger { pointer } => {
                write!(f, "monotonic needs an integer at {pointer}")
            }
        }
    }
}

impl std::error::Error for NormalizeError {}

/// Map one normalizer name of the closed set to its normalizer.
pub fn parse_name(name: &str) -> Result<Normalizer, NormalizeError> {
    match name {
        "uuid_ordinal" => Ok(Normalizer::UuidOrdinal),
        "monotonic" => Ok(Normalizer::Monotonic),
        "timestamp" => Ok(Normalizer::Timestamp),
        "bytes" => Ok(Normalizer::Bytes),
        "none" => Ok(Normalizer::NoOp),
        "set" => Err(NormalizeError::PerTableSet),
        other => Err(NormalizeError::UnknownName(other.to_owned())),
    }
}

/// Redact the `SECRET_KEYS` values of `document` at any depth (the v2 rule, unchanged).
pub fn redact(document: &Value) -> Value {
    let mut redacted = document.clone();
    corpus_v2::redact_secrets(&mut redacted);
    redacted
}

/// Normalize one comparison document: redaction, then `mask` (RFC 6901 pointers replaced by
/// `@mask@`), then each `(pointer, normalizer)` rule in order.
pub fn normalize_document(
    document: &Value,
    mask: &Value,
    rules: &[(String, Normalizer)],
    window: &Window,
) -> Result<Value, NormalizeError> {
    let mut working = redact(document);
    corpus_v2::apply_masks(&mut working, mask);

    // Each rule takes the first leaf pattern it matches, and leaves are visited in document order,
    // so UUID ordinals and monotonic sequences follow the document, not the rule list.
    let mut pointers = Vec::new();
    collect_leaves(&working, "", &mut pointers);
    let mut ordinals = Ordinals::default();
    let mut last_sequence: Vec<Option<i128>> = vec![None; rules.len()];
    let mut edits: Vec<(String, Value)> = Vec::new();
    for pointer in pointers {
        let Some(index) = rules
            .iter()
            .position(|(pattern, _)| pattern_matches(pattern, &pointer))
        else {
            continue;
        };
        let normalizer = rules[index].1;
        let Some(current) = working.pointer(&pointer) else {
            continue;
        };
        match normalizer {
            Normalizer::Bytes | Normalizer::NoOp => {}
            Normalizer::UuidOrdinal => match current {
                Value::String(text) if is_uuid(text) => {
                    let number = ordinals.number(text);
                    edits.push((pointer, json!(format!("@uuid:{number}@"))));
                }
                _ => {}
            },
            Normalizer::Monotonic => {
                let Some(value) = sequence_value(current) else {
                    return Err(NormalizeError::NotInteger { pointer });
                };
                if last_sequence[index].is_some_and(|last| value <= last) {
                    return Err(NormalizeError::NotMonotonic { pointer });
                }
                last_sequence[index] = Some(value);
                edits.push((pointer, json!("@seq@")));
            }
            Normalizer::Timestamp => {
                let parsed = match current {
                    Value::String(text) => OffsetDateTime::parse(text, &Rfc3339).ok(),
                    _ => None,
                };
                if let Some(parsed) = parsed {
                    if parsed < window.start || parsed > window.end {
                        return Err(NormalizeError::OutsideWindow { pointer });
                    }
                    edits.push((pointer, json!("@ts@")));
                }
            }
        }
    }
    for (pointer, value) in edits {
        if let Some(slot) = working.pointer_mut(&pointer) {
            *slot = value;
        }
    }
    Ok(working)
}

/// The integer a monotonic leaf holds: a JSON integer, or text matching `-?[0-9]+`. Floats,
/// booleans and any other text are not sequence values. Integers beyond the `i128` range are
/// refused, where Python integers are unbounded.
fn sequence_value(value: &Value) -> Option<i128> {
    match value {
        Value::Number(number) => number
            .as_i64()
            .map(i128::from)
            .or_else(|| number.as_u64().map(i128::from)),
        Value::String(text) => {
            let digits = text.strip_prefix('-').unwrap_or(text);
            let plain = !digits.is_empty() && digits.bytes().all(|byte| byte.is_ascii_digit());
            if plain {
                text.parse::<i128>().ok()
            } else {
                None
            }
        }
        _ => None,
    }
}

/// Escape one object key as an RFC 6901 reference token.
pub fn escape_token(token: &str) -> String {
    token.replace('~', "~0").replace('/', "~1")
}

/// Every scalar leaf pointer of `value`, in document order.
fn collect_leaves(value: &Value, pointer: &str, out: &mut Vec<String>) {
    match value {
        Value::Object(map) => {
            for (key, child) in map {
                collect_leaves(child, &format!("{pointer}/{}", escape_token(key)), out);
            }
        }
        Value::Array(items) => {
            for (index, child) in items.iter().enumerate() {
                collect_leaves(child, &format!("{pointer}/{index}"), out);
            }
        }
        _ => out.push(pointer.to_owned()),
    }
}

/// Whether a leaf pointer matches a rule pattern, where `*` matches any one segment.
fn pattern_matches(pattern: &str, pointer: &str) -> bool {
    let pattern_tokens: Vec<&str> = pattern.split('/').collect();
    let pointer_tokens: Vec<&str> = pointer.split('/').collect();
    pattern_tokens.len() == pointer_tokens.len()
        && pattern_tokens
            .iter()
            .zip(&pointer_tokens)
            .all(|(token, actual)| *token == "*" || token == actual)
}

/// A canonical UUID: 36 characters, hex digits, with hyphens at 8, 13, 18 and 23.
fn is_uuid(text: &str) -> bool {
    text.len() == 36
        && text.bytes().enumerate().all(|(index, byte)| {
            if matches!(index, 8 | 13 | 18 | 23) {
                byte == b'-'
            } else {
                byte.is_ascii_hexdigit()
            }
        })
}

/// First-appearance numbering of UUIDs within one normalization unit.
#[derive(Default)]
struct Ordinals(BTreeMap<String, usize>);

impl Ordinals {
    fn number(&mut self, text: &str) -> usize {
        let next = self.0.len() + 1;
        *self.0.entry(text.to_owned()).or_insert(next)
    }
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
    fn monotonic_accepts_decimal_text_as_pg_notify_delivers_payloads() {
        // Captures are normalized as one unit; a payload is text, and the value sequence is
        // the one the Python capture carries (config_values_patch.json, pg_notify /payload).
        let rules = [("/side_effects/*/payload".to_owned(), Normalizer::Monotonic)];
        let captures =
            json!({"side_effects": [{"payload": "9"}, {"payload": "10"}, {"payload": "-3"}]});
        let error = normalize_document(&captures, &json!([]), &rules, &one_hour_window())
            .expect_err("-3 does not follow 10");
        assert_eq!(
            error,
            NormalizeError::NotMonotonic {
                pointer: "/side_effects/2/payload".to_owned()
            }
        );

        let rising = json!({"side_effects": [{"payload": "9"}, {"payload": "10"}]});
        assert_eq!(
            normalize_document(&rising, &json!([]), &rules, &one_hour_window())
                .expect("digit text strictly increases"),
            json!({"side_effects": [{"payload": "@seq@"}, {"payload": "@seq@"}]})
        );
    }

    #[test]
    fn monotonic_refuses_values_that_are_not_integers() {
        let rules = [("/seq".to_owned(), Normalizer::Monotonic)];
        for value in [
            json!("1.5"),
            json!("x1"),
            json!(""),
            json!(true),
            json!(2.0),
        ] {
            let error = normalize_document(
                &json!({"seq": value}),
                &json!([]),
                &rules,
                &one_hour_window(),
            )
            .expect_err("not an integer");
            assert_eq!(
                error,
                NormalizeError::NotInteger {
                    pointer: "/seq".to_owned()
                }
            );
        }
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
        let normalized = normalize_document(&document, &json!(["/id"]), &rules, &one_hour_window())
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
