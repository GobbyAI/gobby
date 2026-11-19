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
    /// A normalizer's pointer or column matches no value of its item.
    NoMatch { pointer: String },
    /// A uuid_ordinal value is not a lowercase canonical UUID at this pointer.
    NotUuid { pointer: String },
    /// A timestamp value is not an RFC 3339 string with a UTC offset at this pointer.
    NotTimestamp { pointer: String },
    /// A timestamp's `after` names a column the item does not have.
    AfterMissing { pointer: String },
    /// A timestamp at this pointer is earlier than its `after` column.
    AfterViolated { pointer: String },
    /// A normalizer declaration is neither a closed-set name nor a `{name, after}` object that
    /// the position allows.
    BadSpec(String),
    /// A table's order is neither `set` nor `as_recorded`.
    BadOrder(String),
    /// `monotonic` is declared on a table ordered `set`.
    MonotonicNeedsSequence,
    /// `monotonic` is declared on a document or response value, which is not a sequence unit.
    MonotonicInDocument { pointer: String },
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
                write!(
                    f,
                    "monotonic sequence is not strictly increasing at {pointer}"
                )
            }
            NormalizeError::NotInteger { pointer } => {
                write!(f, "monotonic needs an integer at {pointer}")
            }
            NormalizeError::NoMatch { pointer } => {
                write!(f, "normalizer pointer {pointer} matches nothing")
            }
            NormalizeError::NotUuid { pointer } => {
                write!(f, "uuid_ordinal needs a canonical UUID at {pointer}")
            }
            NormalizeError::NotTimestamp { pointer } => {
                write!(
                    f,
                    "timestamp at {pointer} is not an ISO-8601 timestamp with a UTC offset"
                )
            }
            NormalizeError::AfterMissing { pointer } => {
                write!(f, "the after column of {pointer} does not exist")
            }
            NormalizeError::AfterViolated { pointer } => {
                write!(f, "{pointer} is earlier than its after column")
            }
            NormalizeError::BadSpec(spec) => write!(f, "bad normalizer declaration: {spec}"),
            NormalizeError::BadOrder(order) => {
                write!(f, "order {order:?} must be \"set\" or \"as_recorded\"")
            }
            NormalizeError::MonotonicNeedsSequence => {
                write!(f, "monotonic needs as_recorded order")
            }
            NormalizeError::MonotonicInDocument { pointer } => {
                write!(
                    f,
                    "monotonic needs a sequence unit, not the value at {pointer}"
                )
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
    // An exact pointer must name a leaf (Python: a rule that matches nothing fails the unit); a
    // wildcard pattern (side-effect captures) may match none.
    for (pattern, _) in rules {
        if !pattern.contains('*') && !pointers.iter().any(|pointer| pointer == pattern) {
            return Err(NormalizeError::NoMatch {
                pointer: pattern.clone(),
            });
        }
    }
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
        // A masked leaf is `@mask@` whatever it held: the mask wins over the rule.
        if current == &json!("@mask@") {
            continue;
        }
        match normalizer {
            Normalizer::Bytes | Normalizer::NoOp => {}
            Normalizer::UuidOrdinal => match current {
                Value::String(text) if is_uuid(text) => {
                    let number = ordinals.number(text);
                    edits.push((pointer, json!(format!("@uuid:{number}@"))));
                }
                _ => return Err(NormalizeError::NotUuid { pointer }),
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
                let Some(parsed) = parsed else {
                    return Err(NormalizeError::NotTimestamp { pointer });
                };
                if parsed < window.start || parsed > window.end {
                    return Err(NormalizeError::OutsideWindow { pointer });
                }
                edits.push((pointer, json!("@ts@")));
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

/// A canonical UUID: 36 characters, lowercase hex digits, with hyphens at 8, 13, 18 and 23
/// (`_UUID` of `normalizers_v3.py`, which accepts no uppercase digits).
fn is_uuid(text: &str) -> bool {
    text.len() == 36
        && text.bytes().enumerate().all(|(index, byte)| {
            if matches!(index, 8 | 13 | 18 | 23) {
                byte == b'-'
            } else {
                matches!(byte, b'0'..=b'9' | b'a'..=b'f')
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

/// A field normalizer with its optional `after` sibling (`_spec` of `normalizers_v3.py`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Spec {
    pub normalizer: Normalizer,
    /// The sibling column a `timestamp` may not precede. Only db_delta columns may declare it.
    pub after: Option<String>,
}

/// The order a db_delta table declares. `Set` rows are sorted by key before ordinals run;
/// `AsRecorded` rows are a sequence, which `monotonic` requires.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Order {
    Set,
    AsRecorded,
}

/// Parse a table's declared order.
pub fn parse_order(text: &str) -> Result<Order, NormalizeError> {
    match text {
        "set" => Ok(Order::Set),
        "as_recorded" => Ok(Order::AsRecorded),
        other => Err(NormalizeError::BadOrder(other.to_owned())),
    }
}

/// Parse one normalizer declaration: a closed-set name, or `{"name": ..., "after": ...}` for a
/// timestamp. `allow_after` is true for db_delta columns and false for documents and captures.
pub fn parse_spec(spec: &Value, allow_after: bool) -> Result<Spec, NormalizeError> {
    let refused = || NormalizeError::BadSpec(spec.to_string());
    let (name, after) = match spec {
        Value::String(name) => (name.as_str(), None),
        Value::Object(map) if map.keys().all(|key| key == "name" || key == "after") => {
            let name = map
                .get("name")
                .and_then(Value::as_str)
                .ok_or_else(refused)?;
            let after = match map.get("after") {
                None | Some(Value::Null) => None,
                Some(Value::String(column)) => Some(column.clone()),
                Some(_) => return Err(refused()),
            };
            (name, after)
        }
        _ => return Err(refused()),
    };
    let normalizer = parse_name(name).map_err(|_| refused())?;
    if after.is_some() && (!allow_after || normalizer != Normalizer::Timestamp) {
        return Err(refused());
    }
    Ok(Spec { normalizer, after })
}

/// One db_delta entry's row images as one normalization unit (`normalize_images` of
/// `normalizers_v3.py`). The caller passes the images in the order its delta defines, and they
/// share one `uuid_ordinal` counter. `rules` maps each column to its spec. A rule's column must
/// exist in every image, and each `monotonic` column must strictly increase across the images.
pub fn normalize_images(
    images: &[Value],
    rules: &[(String, Spec)],
    order: Order,
    window: &Window,
) -> Result<Vec<Value>, NormalizeError> {
    if order == Order::Set
        && rules
            .iter()
            .any(|(_, spec)| spec.normalizer == Normalizer::Monotonic)
    {
        return Err(NormalizeError::MonotonicNeedsSequence);
    }
    let mut out = images.to_vec();
    let mut ordinals = Ordinals::default();
    let mut monotonic: Vec<String> = Vec::new();
    for (index, image) in images.iter().enumerate() {
        // Rules run in the image's own column order, as Python runs them in document order.
        let position = |column: &String| {
            image
                .as_object()
                .and_then(|fields| fields.keys().position(|key| key == column))
                .unwrap_or(usize::MAX)
        };
        let mut ordered: Vec<&(String, Spec)> = rules.iter().collect();
        ordered.sort_by_key(|(column, _)| position(column));

        let mut writes: Vec<(&str, Value)> = Vec::new();
        monotonic.clear();
        for (column, spec) in ordered {
            let pointer = format!("rows[{index}]/{column}");
            let value = image
                .get(column.as_str())
                .ok_or_else(|| NormalizeError::NoMatch {
                    pointer: pointer.clone(),
                })?;
            match spec.normalizer {
                Normalizer::Bytes | Normalizer::NoOp => {}
                Normalizer::Monotonic => monotonic.push(column.clone()),
                Normalizer::UuidOrdinal => match value {
                    Value::String(text) if is_uuid(text) => {
                        let number = ordinals.number(text);
                        writes.push((column.as_str(), json!(format!("@uuid:{number}@"))));
                    }
                    _ => return Err(NormalizeError::NotUuid { pointer }),
                },
                Normalizer::Timestamp => {
                    let parsed = timestamp_at(value, &pointer, window)?;
                    if let Some(after) = &spec.after {
                        let sibling = image.get(after.as_str()).ok_or_else(|| {
                            NormalizeError::AfterMissing {
                                pointer: pointer.clone(),
                            }
                        })?;
                        let earlier =
                            timestamp_at(sibling, &format!("rows[{index}]/{after}"), window)?;
                        if parsed < earlier {
                            return Err(NormalizeError::AfterViolated { pointer });
                        }
                    }
                    writes.push((column.as_str(), json!("@ts@")));
                }
            }
        }
        if let Some(fields) = out[index].as_object_mut() {
            for (column, replacement) in writes {
                fields.insert(column.to_owned(), replacement);
            }
        }
    }
    for column in &monotonic {
        let mut previous: Option<i128> = None;
        for (index, image) in images.iter().enumerate() {
            let pointer = format!("rows[{index}]/{column}");
            let value = image
                .get(column.as_str())
                .ok_or_else(|| NormalizeError::NoMatch {
                    pointer: pointer.clone(),
                })?;
            let current = sequence_value(value).ok_or_else(|| NormalizeError::NotInteger {
                pointer: pointer.clone(),
            })?;
            if previous.is_some_and(|last| current <= last) {
                return Err(NormalizeError::NotMonotonic { pointer });
            }
            previous = Some(current);
            if let Some(fields) = out[index].as_object_mut() {
                fields.insert(column.clone(), json!("@seq@"));
            }
        }
    }
    Ok(out)
}

/// The UTC instant of one timestamp value, which must be an RFC 3339 string inside the window.
fn timestamp_at(
    value: &Value,
    pointer: &str,
    window: &Window,
) -> Result<OffsetDateTime, NormalizeError> {
    let parsed = match value {
        Value::String(text) => OffsetDateTime::parse(text, &Rfc3339).ok(),
        _ => None,
    };
    let parsed = parsed.ok_or_else(|| NormalizeError::NotTimestamp {
        pointer: pointer.to_owned(),
    })?;
    if parsed < window.start || parsed > window.end {
        return Err(NormalizeError::OutsideWindow {
            pointer: pointer.to_owned(),
        });
    }
    Ok(parsed)
}

/// Run one normalizer vector (`tests/contracts/normalizer_vectors.json`) as its `kind` says, with
/// its declared key, order, normalizers and mask, and return the normalized value.
pub fn run_vector(vector: &Value, window: &Window) -> Result<Value, NormalizeError> {
    let kind = vector["kind"].as_str().unwrap_or_default();
    let declared = vector["normalizers"]
        .as_object()
        .cloned()
        .unwrap_or_default();
    match kind {
        "rows" => {
            let order = parse_order(vector["order"].as_str().unwrap_or_default())?;
            let key: Vec<&str> = vector["key"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .collect();
            let mut rows = vector["rows"].as_array().cloned().unwrap_or_default();
            if order == Order::Set {
                rows.sort_by_key(|row| {
                    key.iter()
                        .map(|column| row[*column].to_string())
                        .collect::<Vec<String>>()
                });
            }
            let rules = column_rules(&declared, true)?;
            Ok(Value::Array(normalize_images(
                &rows, &rules, order, window,
            )?))
        }
        "captures" => {
            let items = vector["items"].as_array().cloned().unwrap_or_default();
            let rules = column_rules(&declared, false)?;
            Ok(Value::Array(normalize_images(
                &items,
                &rules,
                Order::AsRecorded,
                window,
            )?))
        }
        "document" => {
            let rules = pointer_rules(&declared)?;
            refuse_monotonic_in_document(&rules)?;
            let mask = vector.get("mask").cloned().unwrap_or_else(|| json!([]));
            normalize_document(&vector["document"], &mask, &rules, window)
        }
        other => Err(NormalizeError::UnknownName(other.to_owned())),
    }
}

/// The column rules of one unit. A db_delta row names its column (`db_rows`, where a timestamp may
/// declare `after`); a capture names its field as an RFC 6901 pointer of the item (`/payload`).
fn column_rules(
    declared: &serde_json::Map<String, Value>,
    db_rows: bool,
) -> Result<Vec<(String, Spec)>, NormalizeError> {
    declared
        .iter()
        .map(|(name, spec)| {
            let column = if db_rows {
                name.clone()
            } else {
                name.trim_start_matches('/').to_owned()
            };
            Ok((column, parse_spec(spec, db_rows)?))
        })
        .collect()
}

/// The pointer rules of a document or of the response, each a leaf normalizer.
fn pointer_rules(
    declared: &serde_json::Map<String, Value>,
) -> Result<Vec<(String, Normalizer)>, NormalizeError> {
    declared
        .iter()
        .map(|(pointer, spec)| Ok((pointer.clone(), parse_spec(spec, false)?.normalizer)))
        .collect()
}

/// Refuse `monotonic` in a document: it is a sequence-unit normalizer (Python raises the same).
pub fn refuse_monotonic_in_document(rules: &[(String, Normalizer)]) -> Result<(), NormalizeError> {
    match rules
        .iter()
        .find(|(_, normalizer)| *normalizer == Normalizer::Monotonic)
    {
        Some((pointer, _)) => Err(NormalizeError::MonotonicInDocument {
            pointer: pointer.clone(),
        }),
        None => Ok(()),
    }
}

/// The normalizer vector file replayed through [`run_vector`] (design 7.6, H40).
#[cfg(test)]
mod vector_tests {
    use super::{Window, run_vector};
    use serde_json::Value;
    use std::path::PathBuf;
    use time::OffsetDateTime;
    use time::format_description::well_known::Rfc3339;

    #[test]
    fn normalizer_vectors_replay_through_normalize_rs() {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/contracts/normalizer_vectors.json");
        let file: Value =
            serde_json::from_slice(&std::fs::read(&path).expect("read vectors")).expect("json");
        let at = |key: &str| {
            OffsetDateTime::parse(
                file["window"][key].as_str().expect("window bound"),
                &Rfc3339,
            )
            .expect("window bound is RFC 3339")
        };
        let window = Window {
            start: at("start"),
            end: at("end"),
        };
        let vectors = file["vectors"].as_array().expect("vectors");
        assert_eq!(vectors.len(), 20, "the vector file has 20 vectors");
        for vector in vectors {
            let id = vector["id"].as_str().unwrap_or("vector");
            let result = run_vector(vector, &window);
            match vector["expect"].as_str() {
                Some("pass") => {
                    let normalized = result.unwrap_or_else(|error| panic!("{id}: {error}"));
                    assert_eq!(normalized, vector["normalized"], "{id}");
                }
                Some("fail") => {
                    let error = result.expect_err(&format!("{id} must fail"));
                    let expected = vector["error"]
                        .as_str()
                        .expect("fail vectors name an error");
                    let text = error.to_string();
                    assert!(text.contains(expected), "{id}: {text:?} lacks {expected:?}");
                }
                other => panic!("{id}: unknown expect {other:?}"),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use time::format_description::well_known::Rfc3339;

    const U1: &str = "11111111-1111-4111-8111-111111111111";
    const U2: &str = "22222222-2222-4222-8222-222222222222";
    /// A canonical UUID written in uppercase, which `uuid_ordinal` refuses (`_UUID` is lowercase).
    const UPPER_UUID: &str = "AABBCCDD-EEFF-4011-8122-334455667788";

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
    fn parse_spec_takes_names_and_an_after_only_on_a_db_timestamp() {
        assert_eq!(
            parse_spec(&json!("uuid_ordinal"), false),
            Ok(Spec {
                normalizer: Normalizer::UuidOrdinal,
                after: None
            })
        );
        assert_eq!(
            parse_spec(&json!({"name": "timestamp", "after": "created_at"}), true),
            Ok(Spec {
                normalizer: Normalizer::Timestamp,
                after: Some("created_at".to_owned())
            })
        );
        assert_eq!(
            parse_spec(&json!({"name": "timestamp", "after": null}), true),
            Ok(Spec {
                normalizer: Normalizer::Timestamp,
                after: None
            })
        );
        for refused in [
            (json!({"name": "timestamp", "after": "c"}), false),
            (json!({"name": "uuid_ordinal", "after": "c"}), true),
            (json!("set"), true),
            (json!("bogus"), true),
            (json!({"name": "timestamp", "extra": 1}), true),
            (json!({"after": "c"}), true),
            (json!(7), true),
        ] {
            let (spec, allow_after) = refused;
            assert!(
                matches!(
                    parse_spec(&spec, allow_after),
                    Err(NormalizeError::BadSpec(_))
                ),
                "{spec} must be refused"
            );
        }
    }

    #[test]
    fn parse_order_takes_the_two_declared_orders_only() {
        assert_eq!(parse_order("set"), Ok(Order::Set));
        assert_eq!(parse_order("as_recorded"), Ok(Order::AsRecorded));
        assert_eq!(
            parse_order("sorted"),
            Err(NormalizeError::BadOrder("sorted".to_owned()))
        );
    }

    #[test]
    fn normalize_images_refuses_each_breach_at_its_row_pointer() {
        let window = one_hour_window();
        let uuid_owner = [(
            "owner".to_owned(),
            Spec {
                normalizer: Normalizer::UuidOrdinal,
                after: None,
            },
        )];
        assert_eq!(
            normalize_images(&[json!({"id": 1})], &uuid_owner, Order::Set, &window),
            Err(NormalizeError::NoMatch {
                pointer: "rows[0]/owner".to_owned()
            })
        );
        assert_eq!(
            normalize_images(
                &[json!({"owner": UPPER_UUID})],
                &uuid_owner,
                Order::Set,
                &window
            ),
            Err(NormalizeError::NotUuid {
                pointer: "rows[0]/owner".to_owned()
            })
        );

        let stamp = [(
            "at".to_owned(),
            Spec {
                normalizer: Normalizer::Timestamp,
                after: None,
            },
        )];
        assert_eq!(
            normalize_images(
                &[json!({"at": "2023-11-14T22:30:00"})],
                &stamp,
                Order::Set,
                &window
            ),
            Err(NormalizeError::NotTimestamp {
                pointer: "rows[0]/at".to_owned()
            })
        );
        assert_eq!(
            normalize_images(
                &[json!({"at": "2023-11-15T00:00:00Z"})],
                &stamp,
                Order::Set,
                &window
            ),
            Err(NormalizeError::OutsideWindow {
                pointer: "rows[0]/at".to_owned()
            })
        );

        let after_created = [(
            "at".to_owned(),
            Spec {
                normalizer: Normalizer::Timestamp,
                after: Some("created".to_owned()),
            },
        )];
        assert_eq!(
            normalize_images(
                &[json!({"at": "2023-11-14T22:30:00+00:00"})],
                &after_created,
                Order::Set,
                &window
            ),
            Err(NormalizeError::AfterMissing {
                pointer: "rows[0]/at".to_owned()
            })
        );

        let sequence = [(
            "seq".to_owned(),
            Spec {
                normalizer: Normalizer::Monotonic,
                after: None,
            },
        )];
        assert_eq!(
            normalize_images(&[json!({"seq": 1})], &sequence, Order::Set, &window),
            Err(NormalizeError::MonotonicNeedsSequence)
        );
        assert_eq!(
            normalize_images(
                &[json!({"seq": 2}), json!({"seq": 2})],
                &sequence,
                Order::AsRecorded,
                &window
            ),
            Err(NormalizeError::NotMonotonic {
                pointer: "rows[1]/seq".to_owned()
            })
        );
        assert_eq!(
            normalize_images(
                &[json!({"seq": "9"}), json!({"seq": "10"})],
                &sequence,
                Order::AsRecorded,
                &window
            ),
            Ok(vec![json!({"seq": "@seq@"}), json!({"seq": "@seq@"})])
        );
    }

    #[test]
    fn normalize_images_shares_one_uuid_counter_and_masks_an_after_timestamp() {
        let rules = [
            (
                "owner".to_owned(),
                Spec {
                    normalizer: Normalizer::UuidOrdinal,
                    after: None,
                },
            ),
            (
                "at".to_owned(),
                Spec {
                    normalizer: Normalizer::Timestamp,
                    after: Some("created".to_owned()),
                },
            ),
        ];
        let images = [
            json!({"owner": U2, "created": "2023-11-14T22:10:00+00:00", "at": "2023-11-14T22:20:00+00:00"}),
            json!({"owner": U1, "created": "2023-11-14T22:10:00+00:00", "at": "2023-11-14T22:20:00+00:00"}),
            json!({"owner": U2, "created": "2023-11-14T22:10:00+00:00", "at": "2023-11-14T22:20:00+00:00"}),
        ];
        assert_eq!(
            normalize_images(&images, &rules, Order::Set, &one_hour_window()),
            Ok(vec![
                json!({"owner": "@uuid:1@", "created": "2023-11-14T22:10:00+00:00", "at": "@ts@"}),
                json!({"owner": "@uuid:2@", "created": "2023-11-14T22:10:00+00:00", "at": "@ts@"}),
                json!({"owner": "@uuid:1@", "created": "2023-11-14T22:10:00+00:00", "at": "@ts@"}),
            ])
        );
    }

    #[test]
    fn uuid_ordinals_take_lowercase_canonical_text_only() {
        assert!(is_uuid("aabbccdd-eeff-4011-8122-334455667788"));
        assert!(!is_uuid(UPPER_UUID));
        assert!(!is_uuid("aabbccdd-eeff-4011-8122-33445566778"));
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
