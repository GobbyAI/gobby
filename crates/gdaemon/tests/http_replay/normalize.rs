//! Field normalizers of the corpus v3 closed set.
//!
//! The port of `tests/contracts/normalizers_v3.py`. A normalization unit is one document, one
//! side-effect entry's captures, or one db_delta table's images, and each unit has its own
//! `uuid_ordinal` counter. Every rule is an exact RFC 6901 pointer that must name a scalar leaf of
//! its unit: a pointer that matches nothing fails the unit, and no wildcard exists. `uuid_ordinal`
//! numbers UUIDs by first appearance in document order. `monotonic` masks integers and decimal text
//! (`-?[0-9]+`, as `pg_notify` payloads arrive) and is allowed only in a sequence unit (captures, or
//! a table ordered `as_recorded`); each sequence must strictly increase. `timestamp` masks RFC 3339
//! values inside the run window and fails any value outside it. `bytes` and `none` leave the value
//! unchanged. A document may also declare the collection rule `{"name": "unordered", "key":
//! <field>}` on the pointer of a list, which sorts that list by the JSON text of each item's key
//! field before the field rules and masks apply. The masks of a comparison run after the rules
//! (`http_corpus._comparison`).

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

/// The run window that a `timestamp` value must lie within.
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
    /// A normalizer's pointer or column names no scalar leaf of its unit.
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
    /// A sequence unit's item is neither an object nor an array.
    NotAnObject { pointer: String },
    /// A side-effect entry's recorded value is not a list of captures.
    NotAList { pointer: String },
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
            NormalizeError::NotAnObject { pointer } => write!(f, "{pointer} must be an object"),
            NormalizeError::NotAList { pointer } => write!(f, "{pointer} must be a list"),
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

/// Normalize one document unit (Python `normalize_document`), then apply `mask` (RFC 6901
/// pointers replaced by `@mask@`). Every rule names a scalar leaf, the replacements are computed in
/// document order with one counter, and a monotonic rule is refused: only a sequence unit may
/// carry one. Redaction is the caller's step (Python redacts in `_normalize_response`).
pub fn normalize_document(
    document: &Value,
    mask: &Value,
    rules: &[(String, Normalizer)],
    window: &Window,
) -> Result<Value, NormalizeError> {
    let specs: Vec<(String, Spec)> = rules
        .iter()
        .map(|(pointer, normalizer)| {
            (
                pointer.clone(),
                Spec {
                    normalizer: *normalizer,
                    after: None,
                },
            )
        })
        .collect();
    let mut out = document.clone();
    apply_rules(
        &mut out,
        &specs,
        &mut Ordinals::default(),
        false,
        window,
        "",
    )?;
    corpus_v2::apply_masks(&mut out, mask);
    Ok(out)
}

/// The name of the collection rule `{"name": "unordered", "key": <field>}` (Python
/// `normalizers_v3.unordered_key`).
pub const UNORDERED: &str = "unordered";

/// The key field of an `unordered` collection rule, or `None` when `spec` is not one. A spec named
/// `unordered` must be exactly `{"name": "unordered", "key": <non-empty field>}`.
pub fn parse_unordered(spec: &Value) -> Option<Result<String, NormalizeError>> {
    let map = spec.as_object()?;
    if map.get("name").and_then(Value::as_str) != Some(UNORDERED) {
        return None;
    }
    let key = map
        .get("key")
        .and_then(Value::as_str)
        .filter(|key| !key.is_empty() && map.len() == 2);
    Some(
        key.map(str::to_owned)
            .ok_or_else(|| NormalizeError::BadSpec(spec.to_string())),
    )
}

/// A document's declarations split into its collection rules (pointer and key field) and its
/// field rules, each keyed by RFC 6901 pointer.
pub type DocumentRules = (Vec<(String, String)>, Vec<(String, Normalizer)>);

/// Split a document's declarations (a case's `normalizers`) into collection and field rules.
pub fn document_rules<'a>(
    declared: impl IntoIterator<Item = (&'a String, &'a Value)>,
) -> Result<DocumentRules, NormalizeError> {
    let mut collections = Vec::new();
    let mut fields = Vec::new();
    for (pointer, spec) in declared {
        match parse_unordered(spec) {
            Some(key) => collections.push((pointer.clone(), key?)),
            None => fields.push((pointer.clone(), parse_spec(spec, false)?.normalizer)),
        }
    }
    Ok((collections, fields))
}

/// Sort each list a collection rule names by the JSON text of its items' key field (Python
/// `normalize_document`), outermost pointer first, so the field rules and the masks address the
/// sorted list and row order is never compared.
pub fn sort_collections(
    document: &mut Value,
    collections: &[(String, String)],
) -> Result<(), NormalizeError> {
    let mut ordered: Vec<&(String, String)> = collections.iter().collect();
    ordered.sort_by_key(|(pointer, _)| pointer.matches('/').count());
    for (pointer, key) in ordered {
        let items = document
            .pointer_mut(pointer)
            .ok_or_else(|| NormalizeError::NoMatch {
                pointer: pointer.clone(),
            })?
            .as_array_mut()
            .ok_or_else(|| NormalizeError::NotAList {
                pointer: pointer.clone(),
            })?;
        for (index, item) in items.iter().enumerate() {
            if item.get(key.as_str()).is_none() || !item.is_object() {
                return Err(NormalizeError::NoMatch {
                    pointer: format!("{pointer}/{index}/{}", escape_token(key)),
                });
            }
        }
        items.sort_by_key(|item| item[key.as_str()].to_string());
    }
    Ok(())
}

/// Normalize one sequence unit: each item is a document whose rules are pointers relative to the
/// item (Python `_normalize_items`). The counter is shared by the items, and every `monotonic`
/// pointer is checked across the items in order after all of them have validated.
pub fn normalize_items(
    items: &[Value],
    rules: &[(String, Spec)],
    sequence: bool,
    where_: &str,
    window: &Window,
) -> Result<Vec<Value>, NormalizeError> {
    let mut out = items.to_vec();
    let mut ordinals = Ordinals::default();
    let mut monotonic = Vec::new();
    for (index, item) in out.iter_mut().enumerate() {
        let prefix = format!("{where_}[{index}]");
        if !matches!(item, Value::Object(_) | Value::Array(_)) {
            return Err(NormalizeError::NotAnObject { pointer: prefix });
        }
        monotonic = apply_rules(item, rules, &mut ordinals, sequence, window, &prefix)?;
    }
    check_monotonic(&mut out, &monotonic, where_)?;
    Ok(out)
}

/// Apply one unit's rules to `node` (Python `_apply_rules`). Every pointer must name a scalar leaf
/// of `node`; the checks run in Python's order (the pointer, then the value, then `after`), and the
/// replacements are written only after every rule has validated. Returns the monotonic pointers
/// for the caller's sequence check.
fn apply_rules(
    node: &mut Value,
    rules: &[(String, Spec)],
    ordinals: &mut Ordinals,
    sequence: bool,
    window: &Window,
    prefix: &str,
) -> Result<Vec<String>, NormalizeError> {
    let mut leaves = Vec::new();
    collect_leaves(node, "", &mut leaves);
    for (pointer, _) in rules {
        if !leaves.contains(pointer) {
            return Err(NormalizeError::NoMatch {
                pointer: format!("{prefix}{pointer}"),
            });
        }
    }
    let mut ordered: Vec<&(String, Spec)> = rules.iter().collect();
    ordered.sort_by_key(|(pointer, _)| leaves.iter().position(|leaf| leaf == pointer));

    let mut monotonic = Vec::new();
    let mut writes: Vec<(String, Value)> = Vec::new();
    for (pointer, spec) in ordered {
        let where_ = format!("{prefix}{pointer}");
        let value = node
            .pointer(pointer)
            .ok_or_else(|| NormalizeError::NoMatch {
                pointer: where_.clone(),
            })?;
        match spec.normalizer {
            Normalizer::Bytes | Normalizer::NoOp => {}
            Normalizer::Monotonic => {
                if !sequence {
                    return Err(NormalizeError::MonotonicInDocument { pointer: where_ });
                }
                monotonic.push(pointer.clone());
            }
            Normalizer::UuidOrdinal => match value {
                Value::String(text) if is_uuid(text) => {
                    let number = ordinals.number(text);
                    writes.push((pointer.clone(), json!(format!("@uuid:{number}@"))));
                }
                _ => return Err(NormalizeError::NotUuid { pointer: where_ }),
            },
            Normalizer::Timestamp => {
                let parsed = timestamp_at(value, &where_, window)?;
                if let Some(after) = &spec.after {
                    let sibling =
                        node.get(after.as_str())
                            .ok_or_else(|| NormalizeError::AfterMissing {
                                pointer: where_.clone(),
                            })?;
                    let earlier = timestamp_at(sibling, &format!("{prefix}/{after}"), window)?;
                    if parsed < earlier {
                        return Err(NormalizeError::AfterViolated { pointer: where_ });
                    }
                }
                writes.push((pointer.clone(), json!("@ts@")));
            }
        }
    }
    for (pointer, value) in writes {
        if let Some(slot) = node.pointer_mut(&pointer) {
            *slot = value;
        }
    }
    Ok(monotonic)
}

/// Assert each `monotonic` pointer strictly increases across the items, then mask it (Python
/// `_check_monotonic`). The pointer in an error names the item, as `rows[1]/seq` does.
fn check_monotonic(
    items: &mut [Value],
    pointers: &[String],
    where_: &str,
) -> Result<(), NormalizeError> {
    for pointer in pointers {
        let mut previous: Option<i128> = None;
        for (index, item) in items.iter_mut().enumerate() {
            let where_pointer = format!("{where_}[{index}]{pointer}");
            let current = item
                .pointer(pointer)
                .ok_or_else(|| NormalizeError::NoMatch {
                    pointer: where_pointer.clone(),
                })?;
            let value = sequence_value(current).ok_or_else(|| NormalizeError::NotInteger {
                pointer: where_pointer.clone(),
            })?;
            if previous.is_some_and(|last| value <= last) {
                return Err(NormalizeError::NotMonotonic {
                    pointer: where_pointer,
                });
            }
            previous = Some(value);
            if let Some(slot) = item.pointer_mut(pointer) {
                *slot = json!("@seq@");
            }
        }
    }
    Ok(())
}

/// The integer a `monotonic` leaf holds: a JSON integer or decimal text (`-?[0-9]+`).
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

/// Escape one object key or array index as an RFC 6901 reference token (`_escape`).
pub fn escape_token(token: &str) -> String {
    token.replace('~', "~0").replace('/', "~1")
}

/// Every scalar leaf of `value` as an RFC 6901 pointer, in document order (`_leaf_pointers`).
/// Empty objects and arrays have no leaves, so a rule on them matches nothing.
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

/// A canonical UUID in lowercase only (`_UUID`).
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

/// Normalize one db_delta table's images (Python `normalize_images`): one unit, in the order
/// given. A column is the rule on the pointer `/<column>`, as Python builds it. Only `as_recorded`
/// allows `monotonic`, and its values run across the images in that order.
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
    let pointed: Vec<(String, Spec)> = rules
        .iter()
        .map(|(column, spec)| (format!("/{column}"), spec.clone()))
        .collect();
    normalize_items(images, &pointed, order == Order::AsRecorded, "rows", window)
}

/// Read a timestamp: an RFC 3339 string with a UTC offset, inside the run window.
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

/// Replay one normalizer vector (`tests/contracts/normalizer_vectors.json`) through the same
/// functions the comparison uses.
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
            let rules = column_rules(&declared)?;
            Ok(Value::Array(normalize_images(
                &rows, &rules, order, window,
            )?))
        }
        "captures" => {
            let items = vector["items"].as_array().cloned().unwrap_or_default();
            let rules = pointer_specs(&declared)?;
            Ok(Value::Array(normalize_items(
                &items, &rules, true, "captures", window,
            )?))
        }
        "document" => {
            let (collections, rules) = document_rules(&declared)?;
            let mut document = vector["document"].clone();
            sort_collections(&mut document, &collections)?;
            let mask = vector.get("mask").cloned().unwrap_or_else(|| json!([]));
            normalize_document(&document, &mask, &rules, window)
        }
        other => Err(NormalizeError::UnknownName(other.to_owned())),
    }
}

/// A db_delta table's column declarations, which may declare `after`.
fn column_rules(
    declared: &serde_json::Map<String, Value>,
) -> Result<Vec<(String, Spec)>, NormalizeError> {
    declared
        .iter()
        .map(|(column, spec)| Ok((column.clone(), parse_spec(spec, true)?)))
        .collect()
}

/// A document's or capture item's pointer declarations, keyed by RFC 6901 pointer.
fn pointer_specs(
    declared: &serde_json::Map<String, Value>,
) -> Result<Vec<(String, Spec)>, NormalizeError> {
    declared
        .iter()
        .map(|(pointer, spec)| Ok((pointer.clone(), parse_spec(spec, false)?)))
        .collect()
}

#[cfg(test)]
#[path = "normalize_tests.rs"]
mod tests;

/// The normalizer vector file replayed through [`run_vector`].
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
        assert_eq!(vectors.len(), 21, "the vector file has 21 vectors");
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
