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
    use super::{Kind, load_manifest};
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
}
