//! The v2 HTTP contract corpus (`tests/contracts/http/`): loader, secret redaction, masks and
//! response normalization (design-v2 section 4.2).
//!
//! Copied from `crates/gdaemon/tests/http_contracts.rs` (its loader and normalizer, with the
//! `RouteBackend` parity reader left behind so the crate has no `gobby-*` dependency). The two
//! copies must agree; `http_contracts.rs` switches to this module in the C29 commit.

use std::path::Path;

use serde_json::Value;

/// The corpus directory, relative to this crate: the same depth as gdaemon's test directory.
pub const CORPUS_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../tests/contracts/http");
pub const MASK: &str = "@mask@";
pub const SECRET: &str = "@secret@";
/// Response headers the replay keeps; any other header is dropped before comparison.
pub const RESPONSE_HEADER_ALLOWLIST: &[&str] = &[
    "cache-control",
    "content-type",
    "retry-after",
    "x-gobby-user-id",
    "x-gobby-machine-id",
    "x-gobby-key-id",
];
/// Body keys whose values are replaced with `SECRET` before comparison.
pub const SECRET_KEYS: &[&str] = &[
    "dsn",
    "password",
    "api_key",
    "token",
    "deployment_token",
    "payload_checksum",
    "signature",
    "proof",
];

pub fn read_json(path: &Path) -> Value {
    let text = std::fs::read_to_string(path)
        .unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
    serde_json::from_str(&text).unwrap_or_else(|error| panic!("parse {}: {error}", path.display()))
}

pub fn load_manifest() -> Value {
    read_json(&Path::new(CORPUS_DIR).join("manifest.json"))
}

/// Every manifest case in order, rejecting any whose `schema_version` differs.
pub fn load_all_cases(manifest: &Value) -> Vec<Value> {
    assert_eq!(manifest["schema_version"], 2, "key-cutover corpus version");
    let version = &manifest["schema_version"];
    let families = manifest["families"].as_object().expect("manifest families");
    manifest["cases"]
        .as_array()
        .expect("manifest cases")
        .iter()
        .map(|file| {
            let file = file.as_str().expect("case file name");
            let case = read_json(&Path::new(CORPUS_DIR).join(file));
            assert_eq!(
                &case["schema_version"], version,
                "{file}: schema_version differs from the manifest"
            );
            let family = case["family"].as_str().expect("case family");
            assert!(
                families.contains_key(family),
                "{file}: family {family:?} is not in the manifest"
            );
            assert!(
                matches!(case["backend"].as_str(), Some("up" | "down")),
                "{file}: backend {:?} is neither \"up\" nor \"down\"",
                case["backend"]
            );
            case
        })
        .collect()
}

pub fn redact_secrets(value: &mut Value) {
    match value {
        Value::Object(object) => {
            for (key, item) in object.iter_mut() {
                if SECRET_KEYS.contains(&key.as_str()) {
                    *item = Value::String(SECRET.to_owned());
                } else {
                    redact_secrets(item);
                }
            }
        }
        Value::Array(items) => items.iter_mut().for_each(redact_secrets),
        _ => {}
    }
}

/// Replace each pointed-at value with `MASK`; a pointer to an absent field is a no-op.
pub fn apply_masks(document: &mut Value, pointers: &Value) {
    for pointer in pointers.as_array().expect("mask list") {
        let pointer = pointer.as_str().expect("mask pointer");
        assert!(
            pointer.starts_with('/'),
            "mask pointer {pointer:?} must start with '/'"
        );
        if let Some(value) = document.pointer_mut(pointer) {
            *value = Value::String(MASK.to_owned());
        }
    }
}

/// Redact then mask a `{"response": ...}` document and return its response.
pub fn normalized_response(mut document: Value, mask: &Value) -> Value {
    redact_secrets(&mut document["response"]["body"]);
    apply_masks(&mut document, mask);
    document["response"].take()
}

pub fn is_json(content_type: Option<&str>) -> bool {
    content_type.is_some_and(|value| value.starts_with("application/json"))
}

pub fn percent_encode(text: &str) -> String {
    text.bytes()
        .map(|byte| {
            if byte.is_ascii_alphanumeric() || b"-._~".contains(&byte) {
                char::from(byte).to_string()
            } else {
                format!("%{byte:02X}")
            }
        })
        .collect()
}

/// The request target: the path, then the percent-encoded query in key order.
pub fn request_target(request: &Value) -> String {
    let path = request["path"].as_str().expect("request path");
    let query = request["query"].as_object().expect("request query");
    if query.is_empty() {
        return path.to_owned();
    }
    let pairs: Vec<String> = query
        .iter()
        .map(|(key, value)| {
            let value = value.as_str().expect("query values are strings");
            format!("{}={}", percent_encode(key), percent_encode(value))
        })
        .collect();
    format!("{path}?{}", pairs.join("&"))
}
