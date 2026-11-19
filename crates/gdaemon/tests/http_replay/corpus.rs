//! Reads one corpus case of schema 2 or 3 into the typed view the replay driver uses (design-v2
//! sections 7.5.1, 7.5.2, 7.5.5 and 7.5.6; H1, H14, H44).
//!
//! The recording of a schema 3 case keeps each `db_delta` and `side_effects` value under
//! `recorded` at its declaration index (H44). `recorded_db_delta` and `recorded_side_effects`
//! read those values back in declaration order. Manifest loading and the declared-order and
//! mask-no-match rules of H16 belong to the schema 3 loader, which lands with the replay chunk.

use std::fmt;

use serde_json::{Map, Value};

/// The schema versions the loader accepts (H1): v2 files as they are, and v3 files.
pub const ACCEPTED_SCHEMAS: [u64; 2] = [2, 3];

/// The case kinds (design 7.5.2). Schema 2 cases are all `Http`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Http,
    Mcp,
    Ws,
    Cli,
}

/// One corpus case, with the fields the driver replays and compares.
#[derive(Debug, Clone, PartialEq)]
pub struct Case {
    pub name: String,
    pub schema_version: u64,
    pub kind: Kind,
    pub family: String,
    /// `up` or `down`.
    pub backend: String,
    /// The exact ledger key (H14). Present on every schema 3 case; absent on schema 2 cases.
    pub ledger_key: Option<String>,
    /// The request of an HTTP case, in the recorded request shape.
    pub request: Option<Value>,
    /// The invocation of an MCP, WS or CLI case.
    pub invocation: Option<Value>,
    pub response: Option<Value>,
    /// Each declared `db_delta` entry as written, including its `recorded` value.
    pub db_delta: Vec<Value>,
    /// Each declared `side_effects` entry as written, including its `recorded` value.
    pub side_effects: Vec<Value>,
    /// Field-path normalizers of the case, keyed by RFC 6901 pointer.
    pub normalizers: Map<String, Value>,
    pub mask: Value,
    pub waiver: Option<String>,
    pub native_response: Option<Value>,
}

/// A case file that the loader refuses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CorpusError {
    /// The file's `schema_version` is neither 2 nor 3.
    Schema { case: String, found: Option<u64> },
    /// A key the case's kind requires is missing.
    Field { case: String, field: &'static str },
    /// A key or value disagrees with the case's kind, or a recording has the wrong shape.
    Shape { case: String, detail: String },
}

impl fmt::Display for CorpusError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CorpusError::Schema { case, found } => {
                write!(f, "{case}: schema_version {found:?} is not 2 or 3")
            }
            CorpusError::Field { case, field } => write!(f, "{case}: missing {field}"),
            CorpusError::Shape { case, detail } => write!(f, "{case}: {detail}"),
        }
    }
}

impl std::error::Error for CorpusError {}

/// Parse one case file (schema 2 or 3) into a `Case`.
pub fn parse_case(value: &Value) -> Result<Case, CorpusError> {
    let name = value
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("<unnamed>")
        .to_owned();
    let found = value.get("schema_version").and_then(Value::as_u64);
    let schema_version = match found {
        Some(schema) if ACCEPTED_SCHEMAS.contains(&schema) => schema,
        _ => return Err(CorpusError::Schema { case: name, found }),
    };
    let kind = match value.get("kind").and_then(Value::as_str) {
        Some(text) => parse_kind(&name, text)?,
        None if schema_version == 2 => Kind::Http,
        None => return Err(field(&name, "kind")),
    };
    let family = required_str(value, &name, "family")?;
    let backend = required_str(value, &name, "backend")?;
    if backend != "up" && backend != "down" {
        return Err(shape(
            &name,
            format!("backend {backend:?} is not up or down"),
        ));
    }
    let ledger_key = match value.get("ledger_key") {
        Some(Value::String(key)) => Some(key.clone()),
        Some(_) => return Err(shape(&name, "ledger_key is not a string")),
        None if schema_version == 3 => return Err(field(&name, "ledger_key")),
        None => None,
    };
    if let Some(key) = &ledger_key {
        check_ledger_key(&name, kind, key)?;
    }
    let request = value.get("request").cloned();
    if kind == Kind::Http && request.is_none() {
        return Err(field(&name, "request"));
    }
    let invocation = value.get("invocation").cloned();
    if kind != Kind::Http && invocation.is_none() {
        return Err(field(&name, "invocation"));
    }
    let waiver = optional_str(value, &name, "waiver")?;
    let response = value.get("response").cloned();
    if response.is_none() && waiver.is_none() {
        return Err(field(&name, "response"));
    }
    let normalizers = match value.get("normalizers") {
        None => Map::new(),
        Some(Value::Object(map)) => map.clone(),
        Some(_) => return Err(shape(&name, "normalizers is not an object")),
    };
    if schema_version == 3 {
        for entry in value
            .get("db_delta")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            check_db_delta(&name, entry)?;
        }
        check_masks(&name, value)?;
    }
    Ok(Case {
        schema_version,
        kind,
        family,
        backend,
        ledger_key,
        request,
        invocation,
        response,
        db_delta: entries(value, &name, "db_delta")?,
        side_effects: entries(value, &name, "side_effects")?,
        normalizers,
        mask: value
            .get("mask")
            .cloned()
            .unwrap_or_else(|| Value::Array(Vec::new())),
        waiver,
        native_response: value.get("native_response").cloned(),
        name,
    })
}

/// The schema 3 order rules of one `db_delta` entry (Python `_check_db_delta`, H16): `order` is
/// `set` or `as_recorded`, `as_recorded` needs a non-empty `order_by`, and `set` refuses one.
fn check_db_delta(case: &str, entry: &Value) -> Result<(), CorpusError> {
    let table = entry
        .get("table")
        .and_then(Value::as_str)
        .unwrap_or("<unnamed table>");
    let order = match entry.get("order") {
        None => return Err(shape(case, format!("db_delta {table} order is required"))),
        Some(value) => value
            .as_str()
            .filter(|text| *text == "set" || *text == "as_recorded")
            .ok_or_else(|| {
                shape(
                    case,
                    format!("db_delta {table} order must be 'set' or 'as_recorded'"),
                )
            })?,
    };
    match (order, entry.get("order_by")) {
        ("set", Some(_)) => Err(shape(
            case,
            format!("db_delta {table} order_by is not allowed with order 'set'"),
        )),
        ("as_recorded", order_by)
            if !order_by
                .and_then(Value::as_array)
                .is_some_and(|items| !items.is_empty()) =>
        {
            Err(shape(
                case,
                format!("db_delta {table} order_by is required with order 'as_recorded'"),
            ))
        }
        _ => Ok(()),
    }
}

/// The schema 3 mask rules (Python `_check_mask_shape` and `_check_mask_resolves`, H16): the mask
/// is a list of pointers, each starting with `/` and naming a field of the case.
fn check_masks(case: &str, value: &Value) -> Result<(), CorpusError> {
    let shape_error = || shape(case, "mask must be a list of pointers");
    let mask = value
        .get("mask")
        .cloned()
        .unwrap_or_else(|| Value::Array(Vec::new()));
    for pointer in mask.as_array().ok_or_else(shape_error)? {
        let text = pointer.as_str().ok_or_else(shape_error)?;
        if !text.starts_with('/') {
            return Err(shape(
                case,
                format!("mask pointer '{text}' must start with '/'"),
            ));
        }
        if !resolves(value, text) {
            return Err(shape(
                case,
                format!("mask '{text}' matches no recorded field"),
            ));
        }
    }
    Ok(())
}

/// Whether an RFC 6901 pointer names a field of `document` (Python `_resolves`): a dict key, or a
/// list index written as plain ASCII digits and in range. A sign never resolves as an index.
fn resolves(document: &Value, pointer: &str) -> bool {
    let mut node = document;
    for raw in pointer[1..].split('/') {
        let token = raw.replace("~1", "/").replace("~0", "~");
        node = match node {
            Value::Object(map) => match map.get(&token) {
                Some(child) => child,
                None => return false,
            },
            Value::Array(items) => {
                let plain = !token.is_empty() && token.bytes().all(|byte| byte.is_ascii_digit());
                let index = plain.then(|| token.parse::<usize>().ok()).flatten();
                match index.and_then(|index| items.get(index)) {
                    Some(child) => child,
                    None => return false,
                }
            }
            _ => return false,
        };
    }
    true
}

/// The recorded value of each declared `db_delta` entry, in declaration order.
pub fn recorded_db_delta(case: &Case) -> Result<Vec<Value>, CorpusError> {
    recorded_values(case, &case.db_delta)
}

/// The recorded capture list of each declared `side_effects` entry, in declaration order.
pub fn recorded_side_effects(case: &Case) -> Result<Vec<Value>, CorpusError> {
    recorded_values(case, &case.side_effects)
}

fn field(case: &str, field: &'static str) -> CorpusError {
    CorpusError::Field {
        case: case.to_owned(),
        field,
    }
}

fn shape(case: &str, detail: impl Into<String>) -> CorpusError {
    CorpusError::Shape {
        case: case.to_owned(),
        detail: detail.into(),
    }
}

fn required_str(value: &Value, case: &str, key: &'static str) -> Result<String, CorpusError> {
    value
        .get(key)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| field(case, key))
}

fn optional_str(value: &Value, case: &str, key: &str) -> Result<Option<String>, CorpusError> {
    match value.get(key) {
        None => Ok(None),
        Some(Value::String(text)) => Ok(Some(text.clone())),
        Some(_) => Err(shape(case, format!("{key} is not a string"))),
    }
}

fn entries(value: &Value, case: &str, key: &str) -> Result<Vec<Value>, CorpusError> {
    match value.get(key) {
        None => Ok(Vec::new()),
        Some(Value::Array(items)) => Ok(items.clone()),
        Some(_) => Err(shape(case, format!("{key} is not a list"))),
    }
}

fn parse_kind(case: &str, text: &str) -> Result<Kind, CorpusError> {
    match text {
        "http" => Ok(Kind::Http),
        "mcp" => Ok(Kind::Mcp),
        "ws" => Ok(Kind::Ws),
        "cli" => Ok(Kind::Cli),
        other => Err(shape(
            case,
            format!("kind {other:?} is not http, mcp, ws or cli"),
        )),
    }
}

/// The ledger key prefix of each kind (H14): `HTTP <METHOD> <template>`, `MCP <server>:<tool>`
/// (or `MCP HTTP|stdio gobby:<tool>`), `WS message <type>` and `CLI gobby <verb>`.
fn check_ledger_key(case: &str, kind: Kind, key: &str) -> Result<(), CorpusError> {
    let valid = match kind {
        Kind::Http => {
            let parts: Vec<&str> = key.splitn(3, ' ').collect();
            parts.len() == 3
                && parts[0] == "HTTP"
                && !parts[1].is_empty()
                && parts[1].bytes().all(|byte| byte.is_ascii_uppercase())
                && parts[2].starts_with('/')
        }
        Kind::Mcp => key
            .strip_prefix("MCP ")
            .is_some_and(|rest| rest.contains(':')),
        Kind::Ws => key
            .strip_prefix("WS message ")
            .is_some_and(|rest| !rest.is_empty()),
        Kind::Cli => key
            .strip_prefix("CLI gobby ")
            .is_some_and(|rest| !rest.is_empty()),
    };
    if valid {
        Ok(())
    } else {
        Err(shape(
            case,
            format!("ledger_key {key:?} does not match kind {kind:?}"),
        ))
    }
}

/// The `recorded` value of each declared entry, in declaration order (H44).
fn recorded_values(case: &Case, entries: &[Value]) -> Result<Vec<Value>, CorpusError> {
    entries
        .iter()
        .map(|entry| {
            entry
                .get("recorded")
                .cloned()
                .ok_or_else(|| field(&case.name, "recorded"))
        })
        .collect()
}

/// Every case of the manifest at `path` (schema 2 or 3), each listed file parsed by [`parse_case`].
pub fn load_manifest(path: &std::path::Path) -> Result<Vec<Case>, CorpusError> {
    let label = path.display().to_string();
    let manifest_error = |detail: String| CorpusError::Shape {
        case: label.clone(),
        detail,
    };
    let text = std::fs::read(path).map_err(|error| manifest_error(error.to_string()))?;
    let manifest: Value = serde_json::from_slice(&text)
        .map_err(|error| manifest_error(format!("manifest is not JSON: {error}")))?;
    let found = manifest.get("schema_version").and_then(Value::as_u64);
    match found {
        Some(schema) if ACCEPTED_SCHEMAS.contains(&schema) => {}
        _ => return Err(CorpusError::Schema { case: label, found }),
    }
    let listed = manifest
        .get("cases")
        .and_then(Value::as_array)
        .ok_or_else(|| manifest_error("manifest has no cases array".to_owned()))?;
    let dir = path.parent().unwrap_or_else(|| std::path::Path::new("."));
    listed
        .iter()
        .map(|entry| {
            let file = entry
                .as_str()
                .ok_or_else(|| manifest_error("manifest case is not a file name".to_owned()))?;
            let text = std::fs::read(dir.join(file))
                .map_err(|error| manifest_error(format!("{file}: {error}")))?;
            let value: Value = serde_json::from_slice(&text)
                .map_err(|error| manifest_error(format!("{file} is not JSON: {error}")))?;
            parse_case(&value)
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const CONFIG_PATCH: &str =
        include_str!("../../../../tests/contracts/v3/config_values_patch.json");
    const HEALTH_OK: &str = include_str!("../../../../tests/contracts/http/health_ok.json");

    #[test]
    fn real_v3_recording_reads_its_db_delta_and_side_effects() {
        let value: Value = serde_json::from_str(CONFIG_PATCH).expect("json");
        let case = parse_case(&value).expect("schema 3 case parses");
        assert_eq!(case.schema_version, 3);
        assert_eq!(case.kind, Kind::Http);
        assert_eq!(
            case.ledger_key.as_deref(),
            Some("HTTP PATCH /api/config/values")
        );

        let db = recorded_db_delta(&case).expect("db_delta recorded");
        assert_eq!(db.len(), 2, "one recorded value per declared entry");
        assert_eq!(
            db[0],
            json!({
                "added": [],
                "removed": [],
                "changed": [{"key": {"id": true}, "column": "revision", "before": 1, "after": 2}]
            })
        );

        let effects = recorded_side_effects(&case).expect("side_effects recorded");
        assert_eq!(
            effects,
            vec![json!([{"channel": "gobby_config_changed", "payload": "@seq@"}])],
            "a side effect's recorded value is its capture list"
        );
    }

    #[test]
    fn v2_health_case_parses_as_an_http_case_without_a_ledger_key() {
        let value: Value = serde_json::from_str(HEALTH_OK).expect("json");
        let case = parse_case(&value).expect("schema 2 case parses (H1)");
        assert_eq!(case.schema_version, 2);
        assert_eq!(case.kind, Kind::Http);
        assert_eq!(case.ledger_key, None);
        assert_eq!(case.family, "health");
        assert_eq!(case.backend, "up");
        assert!(case.response.is_some());
        assert!(case.db_delta.is_empty() && case.side_effects.is_empty());
    }

    #[test]
    fn schema_must_be_two_or_three() {
        for schema in [1_u64, 4] {
            let value = json!({
                "schema_version": schema,
                "name": "wrong_schema",
                "kind": "http",
                "family": "health",
                "backend": "up",
                "credential": "none",
                "ledger_key": "HTTP GET /api/health",
                "request": {"method": "GET", "path": "/api/health", "query": {}, "headers": {}, "body": null},
                "response": {"status": 200, "headers": {}, "body": null},
                "mask": []
            });
            let error = parse_case(&value).expect_err("unaccepted schema");
            assert!(
                matches!(error, CorpusError::Schema { .. }),
                "schema {schema}: {error:?}"
            );
        }
    }

    #[test]
    fn ledger_key_prefix_must_match_the_case_kind() {
        let mismatched = json!({
            "schema_version": 3,
            "name": "ws_claims_mcp",
            "kind": "mcp",
            "family": "mcp",
            "backend": "up",
            "credential": "operator",
            "ledger_key": "WS message terminal_list",
            "invocation": {"server": "gobby", "tool": "list_tools", "arguments": {}},
            "response": {"status": 200, "headers": {}, "body": null},
            "mask": []
        });
        let error = parse_case(&mismatched).expect_err("mcp case with a ws key");
        assert!(matches!(error, CorpusError::Shape { .. }), "{error:?}");
    }

    #[test]
    fn a_declared_entry_without_its_recorded_value_is_refused() {
        let value: Value = serde_json::from_str(CONFIG_PATCH).expect("json");
        let mut case = parse_case(&value).expect("schema 3 case parses");
        case.db_delta[0]
            .as_object_mut()
            .expect("entry object")
            .remove("recorded");
        let error = recorded_db_delta(&case).expect_err("no recorded value");
        assert!(matches!(error, CorpusError::Field { .. }), "{error:?}");
    }
}

#[cfg(test)]
mod manifest_tests {
    use super::{Kind, load_manifest, parse_case};
    use serde_json::{Value, json};
    use std::path::PathBuf;

    fn contracts(relative: &str) -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/contracts")
            .join(relative)
    }

    fn listed(path: &PathBuf) -> usize {
        let manifest: Value =
            serde_json::from_slice(&std::fs::read(path).expect("read manifest")).expect("json");
        manifest["cases"].as_array().expect("cases").len()
    }

    #[test]
    fn a_schema_3_manifest_loads_every_listed_case() {
        let path = contracts("v3/manifest.json");
        let cases = load_manifest(&path).expect("the v3 manifest loads");
        assert_eq!(cases.len(), listed(&path));
        assert!(cases.iter().all(|case| case.schema_version == 3));
    }

    #[test]
    fn a_schema_2_manifest_loads_its_sixteen_cases_as_http() {
        let path = contracts("http/manifest.json");
        let cases = load_manifest(&path).expect("the v2 manifest loads");
        assert_eq!(cases.len(), 16);
        assert!(
            cases
                .iter()
                .all(|case| case.schema_version == 2 && case.kind == Kind::Http)
        );
    }

    #[test]
    fn a_manifest_of_another_schema_is_refused() {
        let dir = tempfile::tempdir().expect("manifest dir");
        let path = dir.path().join("manifest.json");
        std::fs::write(&path, json!({"schema_version": 4, "cases": []}).to_string())
            .expect("write manifest");
        assert!(load_manifest(&path).is_err(), "schema 4 is refused");
    }

    /// A minimal schema 3 HTTP case with the given mask, `db_delta` and response body.
    fn v3_case(mask: Value, db_delta: Value, body: Value) -> Value {
        json!({
            "schema_version": 3,
            "name": "h16_probe",
            "kind": "http",
            "family": "health",
            "backend": "up",
            "credential": "none",
            "ledger_key": "HTTP GET /api/health",
            "request": {"method": "GET", "path": "/api/health", "query": {}, "headers": {}, "body": null},
            "response": {"status": 200, "headers": {}, "body": body},
            "db_delta": db_delta,
            "mask": mask
        })
    }

    /// A `db_delta` list holding one table entry with the given extra fields.
    fn db_delta_with(extra: Value) -> Value {
        let mut entry = json!({
            "table": "t",
            "key": ["id"],
            "columns": "all",
            "recorded": {"added": [], "removed": [], "changed": []}
        });
        for (name, value) in extra.as_object().expect("extra fields are an object") {
            entry[name.as_str()] = value.clone();
        }
        json!([entry])
    }

    #[test]
    fn a_db_delta_entry_without_an_order_is_a_loader_error() {
        // Golden (Python corpus_v3._check_db_delta): "db_delta t order is required".
        let value = v3_case(json!([]), db_delta_with(json!({})), json!({}));
        let error = parse_case(&value).expect_err("an undeclared order");
        assert!(
            error.to_string().contains("db_delta t order is required"),
            "{error}"
        );
    }

    #[test]
    fn a_db_delta_order_must_be_set_or_as_recorded() {
        // Golden: "db_delta t order must be 'set' or 'as_recorded'".
        let value = v3_case(
            json!([]),
            db_delta_with(json!({"order": "bogus"})),
            json!({}),
        );
        let error = parse_case(&value).expect_err("an unknown order");
        assert!(
            error
                .to_string()
                .contains("db_delta t order must be 'set' or 'as_recorded'"),
            "{error}"
        );
    }

    #[test]
    fn order_by_is_refused_with_set_and_required_with_as_recorded() {
        // Golden: "db_delta t order_by is not allowed with order 'set'" and "... is required with
        // order 'as_recorded'".
        let set_with_order_by = v3_case(
            json!([]),
            db_delta_with(json!({"order": "set", "order_by": ["id"]})),
            json!({}),
        );
        let error = parse_case(&set_with_order_by).expect_err("set with order_by");
        assert!(
            error
                .to_string()
                .contains("db_delta t order_by is not allowed with order 'set'"),
            "{error}"
        );
        let as_recorded_bare = v3_case(
            json!([]),
            db_delta_with(json!({"order": "as_recorded"})),
            json!({}),
        );
        let error = parse_case(&as_recorded_bare).expect_err("as_recorded without order_by");
        assert!(
            error
                .to_string()
                .contains("db_delta t order_by is required with order 'as_recorded'"),
            "{error}"
        );
    }

    #[test]
    fn a_declared_order_loads() {
        // Golden (Python _check_db_delta): set alone, and as_recorded with order_by, are accepted.
        for extra in [
            json!({"order": "set"}),
            json!({"order": "as_recorded", "order_by": ["id"]}),
        ] {
            let value = v3_case(json!([]), db_delta_with(extra), json!({}));
            parse_case(&value).expect("a declared order loads");
        }
    }

    #[test]
    fn a_mask_pointer_must_resolve_in_a_schema_3_case() {
        // Golden (Python corpus_v3._check_mask_resolves): an unresolved pointer is refused.
        let value = v3_case(json!(["/response/body/x"]), json!([]), json!({}));
        let error = parse_case(&value).expect_err("an unresolved mask");
        assert!(
            error
                .to_string()
                .contains("mask '/response/body/x' matches no recorded field"),
            "{error}"
        );
        let resolved = v3_case(json!(["/response/body/x"]), json!([]), json!({"x": 1}));
        parse_case(&resolved).expect("a resolved mask loads");
    }

    #[test]
    fn a_mask_pointer_must_start_with_a_slash() {
        // Golden (Python): "mask pointer '' must start with '/'".
        let value = v3_case(json!([""]), json!([]), json!({}));
        let error = parse_case(&value).expect_err("an empty pointer");
        assert!(
            error
                .to_string()
                .contains("mask pointer '' must start with '/'"),
            "{error}"
        );
    }

    #[test]
    fn the_slash_pointer_names_the_empty_key_and_matches_nothing() {
        // Golden (Python _resolves): "/" reads the key "", which the case does not have.
        let value = v3_case(json!(["/"]), json!([]), json!({}));
        let error = parse_case(&value).expect_err("the slash pointer");
        assert!(
            error
                .to_string()
                .contains("mask '/' matches no recorded field"),
            "{error}"
        );
    }

    #[test]
    fn list_indexes_resolve_only_as_plain_digits() {
        // Golden (Python _resolves: token.isdigit()): "01" resolves index 1; "+1" does not.
        let leading_zero = v3_case(
            json!(["/response/body/list/01"]),
            json!([]),
            json!({"list": [1, 2]}),
        );
        parse_case(&leading_zero).expect("index 01 resolves to index 1");
        let signed = v3_case(
            json!(["/response/body/list/+1"]),
            json!([]),
            json!({"list": [1, 2]}),
        );
        let error = parse_case(&signed).expect_err("index +1 does not resolve");
        assert!(
            error
                .to_string()
                .contains("mask '/response/body/list/+1' matches no recorded field"),
            "{error}"
        );
    }

    #[test]
    fn dict_keys_and_escaped_tokens_resolve_as_python_resolves_them() {
        // Golden (Python _resolves): a digit token is a dict key on a dict; ~1 is "/" and ~0 is "~".
        let digit_key = v3_case(
            json!(["/response/body/map/0"]),
            json!([]),
            json!({"map": {"0": 1}}),
        );
        parse_case(&digit_key).expect("digit key on a dict resolves");
        let slash_key = v3_case(json!(["/response/body/a~1b"]), json!([]), json!({"a/b": 1}));
        parse_case(&slash_key).expect("~1 escapes a slash");
        let tilde_key = v3_case(json!(["/response/body/a~0b"]), json!([]), json!({"a~b": 1}));
        parse_case(&tilde_key).expect("~0 escapes a tilde");
    }

    #[test]
    fn a_mask_into_a_scalar_matches_no_field() {
        // Golden (Python _resolves): a scalar has no child "x".
        let value = v3_case(json!(["/response/body/n/x"]), json!([]), json!({"n": 1}));
        let error = parse_case(&value).expect_err("a scalar has no children");
        assert!(
            error
                .to_string()
                .contains("mask '/response/body/n/x' matches no recorded field"),
            "{error}"
        );
    }

    #[test]
    fn a_schema_2_mask_that_matches_nothing_stays_a_no_op() {
        // Golden (Python: the schema 2 loader is unchanged): an unresolved schema 2 mask loads.
        let value = json!({
            "schema_version": 2,
            "name": "v2_probe",
            "family": "health",
            "backend": "up",
            "credential": "none",
            "request": {"method": "GET", "path": "/api/health", "query": {}, "headers": {}, "body": null},
            "response": {"status": 200, "headers": {}, "body": {}},
            "mask": ["/response/body/x"]
        });
        parse_case(&value).expect("a schema 2 case loads with an unresolved mask");
    }
}

/// The refusals of the Python loader (`tests/contracts/corpus_v3.py`), one rule per test (fix3 V27).
#[cfg(test)]
mod loader_rule_tests {
    use super::{Kind, load_manifest, parse_case};
    use serde_json::{Value, json};
    use std::path::{Path, PathBuf};

    /// A valid schema 3 HTTP case, in the key order the loader requires.
    fn http_case() -> Value {
        json!({
            "schema_version": 3,
            "name": "rule_probe",
            "kind": "http",
            "family": "health",
            "backend": "up",
            "credential": "none",
            "ledger_key": "HTTP GET /api/health",
            "request": {"method": "GET", "path": "/api/health", "query": {}, "headers": {}, "body": null},
            "response": {"status": 200, "headers": {}, "body": null},
            "mask": []
        })
    }

    /// A valid schema 3 case of `kind` with the given ledger key, invocation and response.
    fn kind_case(kind: &str, key: &str, invocation: Value, response: Value) -> Value {
        json!({
            "schema_version": 3,
            "name": "rule_probe",
            "kind": kind,
            "family": "health",
            "backend": "up",
            "credential": "operator",
            "ledger_key": key,
            "invocation": invocation,
            "response": response,
            "mask": []
        })
    }

    fn refused(value: &Value) -> super::CorpusError {
        parse_case(value).expect_err("the loader refuses this case")
    }

    fn families() -> Value {
        json!({"health": {"parity": "proxy", "origin": "python"}})
    }

    fn write_manifest(dir: &Path, schema: u64, cases: &[&str], families: &Value) -> PathBuf {
        let path = dir.join("manifest.json");
        let manifest = json!({"schema_version": schema, "cases": cases, "families": families});
        std::fs::write(&path, manifest.to_string()).expect("write manifest");
        path
    }

    fn write_case(dir: &Path, file: &str, value: &Value) {
        std::fs::write(dir.join(file), value.to_string()).expect("write case");
    }

    #[test]
    fn a_waived_case_without_a_response_is_refused() {
        // Python _check_response_presence: only a native-extension may omit its response.
        let mut value = http_case();
        value
            .as_object_mut()
            .expect("case object")
            .shift_remove("response");
        value["waiver"] = json!("probe.http.waived");
        let error = refused(&value);
        assert!(error.to_string().contains("missing response"), "{error}");
    }

    #[test]
    fn a_native_extension_case_refuses_a_response() {
        // Python _check_response_presence: a native-extension cannot carry a response.
        let mut value = http_case();
        value["waiver"] = json!("probe.http.native-extension");
        value["native_response"] = json!({"status": 200, "headers": {}, "body": null});
        let error = refused(&value);
        assert!(error.to_string().contains("native-extension"), "{error}");
    }

    #[test]
    fn a_keyed_mcp_transport_must_be_the_invocation_transport() {
        // Python V7: "MCP stdio gobby:<tool>" names the stdio transport of its invocation.
        let response = json!({"jsonrpc": "2.0", "id": 1, "result": {}});
        let mismatched = kind_case(
            "mcp",
            "MCP stdio gobby:list_tools",
            json!({"transport": "http", "server": "gobby", "tool": "list_tools", "arguments": {}}),
            response.clone(),
        );
        let error = refused(&mismatched);
        assert!(error.to_string().contains("transport"), "{error}");
        let matching = kind_case(
            "mcp",
            "MCP HTTP gobby:list_tools",
            json!({"transport": "http", "server": "gobby", "tool": "list_tools", "arguments": {}}),
            response,
        );
        parse_case(&matching).expect("a matching transport loads");
    }

    #[test]
    fn a_cli_key_verb_must_be_the_first_argument() {
        let value = kind_case(
            "cli",
            "CLI gobby schema",
            json!({"argv": ["other", "--help"], "stdin": null}),
            json!({"exit": 0, "stdout": "", "stderr": ""}),
        );
        let error = refused(&value);
        assert!(error.to_string().contains("argv[0]"), "{error}");
    }

    #[test]
    fn a_ws_frame_type_must_equal_the_key_type() {
        let value = kind_case(
            "ws",
            "WS message terminal_list",
            json!({"frames": ["{\"type\": \"other\"}"]}),
            json!(["{\"type\": \"terminal_list\"}"]),
        );
        let error = refused(&value);
        assert!(error.to_string().contains("frame type"), "{error}");
    }

    #[test]
    fn an_http_key_method_must_equal_the_request_method() {
        let mut value = http_case();
        value["ledger_key"] = json!("HTTP POST /api/health");
        let error = refused(&value);
        assert!(error.to_string().contains("method"), "{error}");
    }

    #[test]
    fn keys_must_follow_the_declared_order() {
        let value = json!({
            "schema_version": 3, "name": "rule_probe", "kind": "http", "family": "health",
            "backend": "up", "credential": "none", "ledger_key": "HTTP GET /api/health",
            "request": {"method": "GET", "path": "/api/health", "query": {}, "headers": {}, "body": null},
            "mask": [], "response": {"status": 200, "headers": {}, "body": null}
        });
        let error = refused(&value);
        assert!(error.to_string().contains("key order"), "{error}");
    }

    #[test]
    fn an_unknown_key_is_refused() {
        let mut value = http_case();
        value["extra"] = json!(1);
        let error = refused(&value);
        assert!(error.to_string().contains("unknown key"), "{error}");
    }

    #[test]
    fn a_ws_response_must_be_a_list_of_text_frames() {
        let value = kind_case(
            "ws",
            "WS message terminal_list",
            json!({"frames": ["{\"type\": \"terminal_list\"}"]}),
            json!({"status": 200}),
        );
        let error = refused(&value);
        assert!(error.to_string().contains("wrong shape"), "{error}");
    }

    #[test]
    fn an_mcp_response_must_be_a_jsonrpc_message() {
        let value = kind_case(
            "mcp",
            "MCP HTTP gobby:list_tools",
            json!({"transport": "http", "server": "gobby", "tool": "list_tools", "arguments": {}}),
            json!({"status": 200, "headers": {}, "body": null}),
        );
        let error = refused(&value);
        assert!(error.to_string().contains("wrong shape"), "{error}");
    }

    #[test]
    fn a_normalizer_must_name_a_field_normalizer() {
        let value = json!({
            "schema_version": 3, "name": "rule_probe", "kind": "http", "family": "health",
            "backend": "up", "credential": "none", "ledger_key": "HTTP GET /api/health",
            "request": {"method": "GET", "path": "/api/health", "query": {}, "headers": {}, "body": null},
            "response": {"status": 200, "headers": {}, "body": null},
            "normalizers": {"/response/body": "bogus"},
            "mask": []
        });
        let error = refused(&value);
        assert!(error.to_string().contains("bogus"), "{error}");
    }

    #[test]
    fn a_side_effect_must_name_a_known_channel() {
        let value = json!({
            "schema_version": 3, "name": "rule_probe", "kind": "http", "family": "health",
            "backend": "up", "credential": "none", "ledger_key": "HTTP GET /api/health",
            "request": {"method": "GET", "path": "/api/health", "query": {}, "headers": {}, "body": null},
            "response": {"status": 200, "headers": {}, "body": null},
            "side_effects": [{"channel": "bogus", "match": {}}],
            "mask": []
        });
        let error = refused(&value);
        assert!(error.to_string().contains("channel"), "{error}");
    }

    #[test]
    fn a_seed_step_is_an_http_or_sql_object() {
        let value = json!({
            "schema_version": 3, "name": "rule_probe", "kind": "http", "family": "health",
            "backend": "up", "credential": "none", "ledger_key": "HTTP GET /api/health",
            "request": {"method": "GET", "path": "/api/health", "query": {}, "headers": {}, "body": null},
            "seed": [{"bogus": {}}],
            "response": {"status": 200, "headers": {}, "body": null},
            "mask": []
        });
        let error = refused(&value);
        assert!(error.to_string().contains("seed"), "{error}");
    }

    #[test]
    fn a_db_delta_key_must_be_non_empty() {
        let value = json!({
            "schema_version": 3, "name": "rule_probe", "kind": "http", "family": "health",
            "backend": "up", "credential": "none", "ledger_key": "HTTP GET /api/health",
            "request": {"method": "GET", "path": "/api/health", "query": {}, "headers": {}, "body": null},
            "response": {"status": 200, "headers": {}, "body": null},
            "db_delta": [{"table": "t", "key": [], "columns": "all", "order": "set",
                          "recorded": {"added": [], "removed": [], "changed": []}}],
            "mask": []
        });
        let error = refused(&value);
        assert!(error.to_string().contains("key is required"), "{error}");
    }

    #[test]
    fn a_schema_2_case_carries_no_kind() {
        let mut value = http_case();
        value["schema_version"] = json!(2);
        value.as_object_mut().expect("case object").shift_remove("ledger_key");
        let error = refused(&value);
        assert!(error.to_string().contains("no kind"), "{error}");
    }

    #[test]
    fn a_mcp_case_carries_no_request() {
        let mut value = kind_case(
            "mcp",
            "MCP HTTP gobby:list_tools",
            json!({"transport": "http", "server": "gobby", "tool": "list_tools", "arguments": {}}),
            json!({"jsonrpc": "2.0", "id": 1, "result": {}}),
        );
        value["request"] = json!({"method": "POST", "path": "/api/mcp/gobby/tools/list_tools"});
        let error = refused(&value);
        assert!(error.to_string().contains("request is only for kind http"), "{error}");
    }

    #[test]
    fn a_case_name_must_equal_its_file_stem() {
        let dir = tempfile::tempdir().expect("dir");
        let mut value = http_case();
        value["name"] = json!("other_name");
        write_case(dir.path(), "a.json", &value);
        let path = write_manifest(dir.path(), 3, &["a.json"], &families());
        let error = load_manifest(&path).expect_err("the name differs from the file stem");
        assert!(error.to_string().contains("file stem"), "{error}");
    }

    #[test]
    fn a_case_family_must_be_in_the_manifest() {
        let dir = tempfile::tempdir().expect("dir");
        let mut value = http_case();
        value["name"] = json!("a");
        write_case(dir.path(), "a.json", &value);
        let path = write_manifest(dir.path(), 3, &["a.json"], &json!({}));
        let error = load_manifest(&path).expect_err("the family is not in the manifest");
        assert!(error.to_string().contains("not in the manifest"), "{error}");
    }

    #[test]
    fn a_manifest_family_needs_a_known_parity_and_origin() {
        let dir = tempfile::tempdir().expect("dir");
        let mut value = http_case();
        value["name"] = json!("a");
        write_case(dir.path(), "a.json", &value);
        let families = json!({"health": {"parity": "bogus", "origin": "python"}});
        let path = write_manifest(dir.path(), 3, &["a.json"], &families);
        assert!(load_manifest(&path).is_err(), "an unknown parity is refused");
    }

    #[test]
    fn a_v3_manifest_must_list_every_case_file() {
        // Python V6: a case file in the v3 directory that the manifest does not list is refused.
        let dir = tempfile::tempdir().expect("dir");
        let mut listed = http_case();
        listed["name"] = json!("a");
        write_case(dir.path(), "a.json", &listed);
        let mut unlisted = http_case();
        unlisted["name"] = json!("b");
        write_case(dir.path(), "b.json", &unlisted);
        let path = write_manifest(dir.path(), 3, &["a.json"], &families());
        let error = load_manifest(&path).expect_err("b.json is not listed");
        assert!(error.to_string().contains("b.json"), "{error}");
    }

    #[test]
    fn a_v3_manifest_ignores_a_json_file_that_is_not_a_case() {
        // grant_encoding.json is a vector of the v3 directory, not a case: it carries no ledger_key.
        let dir = tempfile::tempdir().expect("dir");
        let mut listed = http_case();
        listed["name"] = json!("a");
        write_case(dir.path(), "a.json", &listed);
        write_case(dir.path(), "vector.json", &json!({"schema_version": 3, "description": "x"}));
        let path = write_manifest(dir.path(), 3, &["a.json"], &families());
        let cases = load_manifest(&path).expect("the vector is not a case");
        assert_eq!(cases.len(), 1);
        assert_eq!(cases[0].kind, Kind::Http);
    }

    #[test]
    fn the_shipped_v3_manifest_lists_every_case_file() {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/contracts/v3/manifest.json");
        let cases = load_manifest(&path).expect("the shipped v3 manifest loads");
        assert_eq!(cases.len(), 5);
    }
}
