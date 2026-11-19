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
    unimplemented!("not wired: corpus::parse_case")
}

/// The recorded value of each declared `db_delta` entry, in declaration order.
pub fn recorded_db_delta(case: &Case) -> Result<Vec<Value>, CorpusError> {
    unimplemented!("not wired: corpus::recorded_db_delta")
}

/// The recorded capture list of each declared `side_effects` entry, in declaration order.
pub fn recorded_side_effects(case: &Case) -> Result<Vec<Value>, CorpusError> {
    unimplemented!("not wired: corpus::recorded_side_effects")
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
        assert_eq!(case.ledger_key.as_deref(), Some("HTTP PATCH /api/config/values"));

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
