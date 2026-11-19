//! Reads one corpus case of schema 2 or 3 into the typed view the replay driver uses.
//!
//! The loader refuses every case shape that the Python loader refuses (`tests/contracts/
//! corpus_v3.py`). `parse_case` checks one case's own shape. `load_manifest` adds the
//! file-level rules: the file stem, the family and the manifest's families, and, for a schema 3
//! manifest, that it lists every case file of its directory.
//!
//! The recording of a schema 3 case keeps each `db_delta` and `side_effects` value under
//! `recorded` at its declaration index. `recorded_db_delta` and `recorded_side_effects`
//! read those values back in declaration order.

use std::fmt;
use std::path::Path;

use gobby_daemon::front_door::waivers::is_native_extension;
use serde_json::{Map, Value, json};

/// The schema versions the loader accepts: v2 files as they are, and v3 files.
pub const ACCEPTED_SCHEMAS: [u64; 2] = [2, 3];

/// The manifest file name in every corpus directory.
const MANIFEST_FILE: &str = "manifest.json";

/// JSON vectors kept beside the schema 3 cases. Tests read them; the loader never replays them
/// (Python `V3_NON_CASE_FILES`).
const V3_NON_CASE_FILES: [&str; 1] = ["grant_encoding.json"];

/// Schema 3 keys in the order the loader requires (Python `V3_KEY_ORDER`).
const V3_KEY_ORDER: [&str; 17] = [
    "schema_version",
    "name",
    "kind",
    "family",
    "backend",
    "credential",
    "ledger_key",
    "request",
    "invocation",
    "seed",
    "response",
    "db_delta",
    "side_effects",
    "normalizers",
    "mask",
    "waiver",
    "native_response",
];

/// The schema 3 keys every case carries (Python `V3_REQUIRED_KEYS`).
const V3_REQUIRED_KEYS: [&str; 8] = [
    "schema_version",
    "name",
    "kind",
    "family",
    "backend",
    "credential",
    "ledger_key",
    "mask",
];

const CREDENTIALS: [&str; 3] = ["none", "operator", "grant"];
const CHANNELS: [&str; 3] = ["pg_notify", "ws_publish", "files_home_write"];
const FIELD_NORMALIZERS: [&str; 5] = ["uuid_ordinal", "monotonic", "timestamp", "bytes", "none"];
const MCP_TRANSPORTS: [&str; 2] = ["http", "stdio"];
const PARITY_VALUES: [&str; 2] = ["proxy", "native"];
const ORIGIN_VALUES: [&str; 2] = ["python", "gdaemon"];

/// The case kinds. Schema 2 cases are all `Http`.
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
    /// The exact ledger key. Present on every schema 3 case; absent on schema 2 cases.
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

/// Parse one case file (schema 2 or 3) into a `Case`: the case's own shape, by the rules of the
/// Python loader. The file-level rules (stem, family, listing) are checked by [`load_manifest`].
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
    let object = value
        .as_object()
        .ok_or_else(|| shape(&name, "the case is not a JSON object"))?;
    if schema_version == 3 {
        check_v3_keys(object, &name)?;
    }
    let kind = match (schema_version, object.get("kind")) {
        (2, Some(_)) => return Err(shape(&name, "a schema 2 case carries no kind")),
        (2, None) => Kind::Http,
        _ => parse_kind(&name, &required_str(value, &name, "kind")?)?,
    };
    required_str(value, &name, "name")?;
    let family = required_str(value, &name, "family")?;
    let backend = required_str(value, &name, "backend")?;
    if backend != "up" && backend != "down" {
        return Err(shape(
            &name,
            format!("backend {backend:?} is neither 'up' nor 'down'"),
        ));
    }
    let credential = required_str(value, &name, "credential")?;
    if !CREDENTIALS.contains(&credential.as_str()) {
        return Err(shape(
            &name,
            format!("credential {credential:?} is unknown"),
        ));
    }
    let ledger_key = match value.get("ledger_key") {
        None if schema_version == 3 => return Err(field(&name, "ledger_key")),
        None => None,
        Some(Value::String(key)) => Some(key.clone()),
        Some(_) => return Err(shape(&name, "ledger_key is not a string")),
    };
    let request = value.get("request").cloned();
    let invocation = value.get("invocation").cloned();
    let response = value.get("response").cloned();
    let waiver = optional_str(value, &name, "waiver")?;
    let mask = value
        .get("mask")
        .cloned()
        .ok_or_else(|| field(&name, "mask"))?;
    check_mask_shape(&mask, &name)?;
    let native_extension = waiver.as_deref().is_some_and(is_native_extension);
    if schema_version == 3 {
        match (native_extension, response.is_some()) {
            (true, true) => {
                return Err(shape(
                    &name,
                    "a native-extension waiver cannot carry a response",
                ));
            }
            (false, false) => return Err(field(&name, "response")),
            _ => {}
        }
        if kind == Kind::Http {
            if request.is_none() {
                return Err(field(&name, "request"));
            }
            if invocation.is_some() {
                return Err(shape(&name, "invocation is only for kinds mcp, ws and cli"));
            }
        } else {
            if request.is_some() {
                return Err(shape(&name, "request is only for kind http"));
            }
            if invocation.is_none() {
                return Err(shape(
                    &name,
                    format!("invocation is required for kind {}", kind_label(kind)),
                ));
            }
        }
    } else {
        if response.is_none() {
            return Err(field(&name, "response"));
        }
        if kind == Kind::Http && request.is_none() {
            return Err(field(&name, "request"));
        }
    }
    if let Some(request) = &request
        && kind == Kind::Http
    {
        check_request_shape(request, &name)?;
    }
    if let Some(key) = &ledger_key
        && schema_version == 3
    {
        check_ledger_key(&name, kind, key, request.as_ref(), invocation.as_ref())?;
    }
    if let Some(response) = &response
        && schema_version == 3
    {
        check_response(&name, kind, response)?;
    }
    if schema_version == 3 {
        check_v3_optional(value, &name)?;
        check_mask_resolves(value, &name)?;
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
        mask,
        waiver,
        native_response: value.get("native_response").cloned(),
        name,
    })
}

/// The schema 3 key rules (Python `_validate_schema_3`): no unknown key, every required key present,
/// and the keys in the declared order.
fn check_v3_keys(object: &Map<String, Value>, case: &str) -> Result<(), CorpusError> {
    if let Some(unknown) = object
        .keys()
        .find(|key| !V3_KEY_ORDER.contains(&key.as_str()))
    {
        return Err(shape(case, format!("unknown key {unknown:?}")));
    }
    if let Some(&missing) = V3_REQUIRED_KEYS
        .iter()
        .find(|key| !object.contains_key(**key))
    {
        return Err(field(case, missing));
    }
    let positions: Vec<usize> = object
        .keys()
        .filter_map(|key| V3_KEY_ORDER.iter().position(|known| *known == key.as_str()))
        .collect();
    if positions.windows(2).any(|pair| pair[0] > pair[1]) {
        return Err(shape(case, "keys are out of key order"));
    }
    Ok(())
}

/// The schema 3 optional-key rules (Python `_check_optional` and the entry checks): `seed` steps,
/// `normalizers`, every `db_delta` entry and every `side_effects` entry.
fn check_v3_optional(value: &Value, case: &str) -> Result<(), CorpusError> {
    if let Some(seed) = value.get("seed") {
        let steps = seed
            .as_array()
            .ok_or_else(|| shape(case, "each seed step is one http or sql object"))?;
        if !steps.iter().all(is_seed_step) {
            return Err(shape(case, "each seed step is one http or sql object"));
        }
    }
    if let Some(normalizers) = value.get("normalizers") {
        check_normalizers(normalizers, case, false)?;
        check_pointer_keys(normalizers, case)?;
    }
    for entry in entries(value, case, "db_delta")? {
        check_db_delta(case, &entry)?;
    }
    for effect in entries(value, case, "side_effects")? {
        check_side_effect(case, &effect)?;
    }
    Ok(())
}

fn is_seed_step(step: &Value) -> bool {
    step.as_object()
        .is_some_and(|map| map.len() == 1 && map.keys().all(|key| key == "http" || key == "sql"))
}

/// The schema 3 order rules of one `db_delta` entry (Python `_check_db_delta`): the table, the
/// key and the columns are declared, `order` is `set` or `as_recorded`, `as_recorded` needs a
/// non-empty `order_by`, and `set` refuses one.
fn check_db_delta(case: &str, entry: &Value) -> Result<(), CorpusError> {
    let object = entry
        .as_object()
        .ok_or_else(|| shape(case, "db_delta entries must be objects"))?;
    let table = object
        .get("table")
        .and_then(Value::as_str)
        .filter(|table| !table.is_empty())
        .ok_or_else(|| shape(case, "db_delta table is required"))?;
    let key_declared = object
        .get("key")
        .and_then(Value::as_array)
        .is_some_and(|columns| !columns.is_empty() && columns.iter().all(Value::is_string));
    if !key_declared {
        return Err(shape(case, format!("db_delta {table} key is required")));
    }
    let columns_declared = match object.get("columns") {
        Some(Value::String(text)) => text == "all",
        Some(Value::Array(columns)) => !columns.is_empty() && columns.iter().all(Value::is_string),
        _ => false,
    };
    if !columns_declared {
        return Err(shape(
            case,
            format!("db_delta {table} columns must be 'all' or a list"),
        ));
    }
    if let Some(normalizers) = object.get("normalizers") {
        check_normalizers(normalizers, case, true)?;
    }
    let order = match object.get("order") {
        None => return Err(shape(case, format!("db_delta {table} order is required"))),
        Some(Value::String(order)) if order == "set" || order == "as_recorded" => order.as_str(),
        Some(_) => {
            return Err(shape(
                case,
                format!("db_delta {table} order must be 'set' or 'as_recorded'"),
            ));
        }
    };
    match (order, object.get("order_by")) {
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

/// One `side_effects` entry (Python `_check_side_effect`): a known channel, a `match` object and
/// valid normalizers keyed by pointer.
fn check_side_effect(case: &str, effect: &Value) -> Result<(), CorpusError> {
    let object = effect.as_object();
    let channel_known = object
        .and_then(|map| map.get("channel"))
        .and_then(Value::as_str)
        .is_some_and(|channel| CHANNELS.contains(&channel));
    let match_declared = object.is_some_and(|map| map.get("match").is_some_and(Value::is_object));
    if !channel_known || !match_declared {
        return Err(shape(
            case,
            "side effect needs a known channel and a match object",
        ));
    }
    if let Some(normalizers) = object.and_then(|map| map.get("normalizers")) {
        check_normalizers(normalizers, case, false)?;
        check_pointer_keys(normalizers, case)?;
    }
    Ok(())
}

/// A normalizer map (Python `_check_normalizers`): each value names a field normalizer, or is
/// `{"name", "after"}` where `after` is legal only on a `db_delta` normalizer and only for timestamps.
fn check_normalizers(
    normalizers: &Value,
    case: &str,
    allow_after: bool,
) -> Result<(), CorpusError> {
    let map = normalizers
        .as_object()
        .ok_or_else(|| shape(case, "normalizers must be an object"))?;
    for spec in map.values() {
        let (_, after) = normalizer_spec(spec, case)?;
        if after.is_some() && !allow_after {
            return Err(shape(
                case,
                "'after' is declared only on db_delta normalizers",
            ));
        }
    }
    Ok(())
}

/// One normalizer spec: its field normalizer name and its `after` column, if any.
fn normalizer_spec(spec: &Value, case: &str) -> Result<(String, Option<String>), CorpusError> {
    let (name, after) = match spec {
        Value::String(name) => (name.clone(), None),
        Value::Object(map)
            if map.contains_key("name")
                && map.keys().all(|key| key == "name" || key == "after") =>
        {
            let after = match map.get("after") {
                None | Some(Value::Null) => None,
                Some(Value::String(column)) if !column.is_empty() => Some(column.clone()),
                Some(_) => return Err(shape(case, "normalizer 'after' must name a column")),
            };
            (map["name"].as_str().unwrap_or_default().to_owned(), after)
        }
        other => {
            return Err(shape(
                case,
                format!("normalizer {other} is not a field normalizer"),
            ));
        }
    };
    if !FIELD_NORMALIZERS.contains(&name.as_str()) {
        return Err(shape(
            case,
            format!("normalizer {name:?} is not a field normalizer"),
        ));
    }
    if after.is_some() && name != "timestamp" {
        return Err(shape(case, "only the timestamp normalizer takes 'after'"));
    }
    Ok((name, after))
}

/// Case-level and side-effect normalizer keys are RFC 6901 pointers (Python `_check_pointer_keys`).
fn check_pointer_keys(normalizers: &Value, case: &str) -> Result<(), CorpusError> {
    for key in normalizers.as_object().into_iter().flat_map(Map::keys) {
        if !key.starts_with('/') {
            return Err(shape(
                case,
                format!("normalizer key {key:?} must be a JSON pointer"),
            ));
        }
    }
    Ok(())
}

fn check_request_shape(request: &Value, case: &str) -> Result<(), CorpusError> {
    if request.get("method").is_some_and(Value::is_string)
        && request.get("path").is_some_and(Value::is_string)
    {
        Ok(())
    } else {
        Err(shape(case, "request needs a method and a path"))
    }
}

fn check_mask_shape(mask: &Value, case: &str) -> Result<(), CorpusError> {
    if mask
        .as_array()
        .is_some_and(|pointers| pointers.iter().all(Value::is_string))
    {
        Ok(())
    } else {
        Err(shape(case, "mask must be a list of pointers"))
    }
}

/// The schema 3 mask rule (Python `_check_mask_resolves`): each pointer starts with `/` and names a
/// field of the case.
fn check_mask_resolves(value: &Value, case: &str) -> Result<(), CorpusError> {
    let mask = value
        .get("mask")
        .and_then(Value::as_array)
        .into_iter()
        .flatten();
    for pointer in mask {
        let text = pointer.as_str().unwrap_or_default();
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

fn kind_label(kind: Kind) -> &'static str {
    match kind {
        Kind::Http => "http",
        Kind::Mcp => "mcp",
        Kind::Ws => "ws",
        Kind::Cli => "cli",
    }
}

/// The parts of one ledger key (Python `_KEY_PATTERNS`, matched whole): the HTTP method, the MCP
/// transport token (`HTTP` or `stdio`), server and tool, the WS frame type and the CLI verb.
#[derive(Debug, PartialEq, Eq)]
enum KeyParts<'a> {
    Http {
        method: &'a str,
    },
    Mcp {
        transport: Option<&'a str>,
        server: &'a str,
        tool: &'a str,
    },
    Ws {
        frame_type: &'a str,
    },
    Cli {
        verb: &'a str,
    },
}

/// Parses a ledger key of `kind`: `HTTP <METHOD> <template>`, `MCP <server>:<tool>` or
/// `MCP HTTP|stdio <server>:<tool>`, `WS message <type>` and `CLI gobby <verb>`. A keyed MCP name
/// other than `gobby` is refused (Python `_check_ledger_key`).
fn parse_key<'a>(case: &str, kind: Kind, key: &'a str) -> Result<KeyParts<'a>, CorpusError> {
    let bad = || {
        shape(
            case,
            format!("ledger_key {key:?} is not a {} key", kind_label(kind)),
        )
    };
    let plain = |text: &str| !text.is_empty() && !text.contains(char::is_whitespace);
    match kind {
        Kind::Http => {
            let (method, template) = key
                .strip_prefix("HTTP ")
                .and_then(|rest| rest.split_once(' '))
                .ok_or_else(&bad)?;
            let method_ok = !method.is_empty() && method.bytes().all(|b| b.is_ascii_uppercase());
            if !method_ok || !template.starts_with('/') || !plain(template) {
                return Err(bad());
            }
            Ok(KeyParts::Http { method })
        }
        Kind::Mcp => {
            let rest = key.strip_prefix("MCP ").ok_or_else(&bad)?;
            let (transport, named) = if let Some(named) = rest.strip_prefix("HTTP ") {
                (Some("HTTP"), named)
            } else if let Some(named) = rest.strip_prefix("stdio ") {
                (Some("stdio"), named)
            } else {
                (None, rest)
            };
            let (server, tool) = named.split_once(':').ok_or_else(&bad)?;
            if !plain(server) || !plain(tool) {
                return Err(bad());
            }
            if transport.is_some() && server != "gobby" {
                return Err(shape(
                    case,
                    format!("ledger_key {key:?} needs the gobby server"),
                ));
            }
            Ok(KeyParts::Mcp {
                transport,
                server,
                tool,
            })
        }
        Kind::Ws => {
            let frame_type = key
                .strip_prefix("WS message ")
                .filter(|rest| plain(rest))
                .ok_or_else(&bad)?;
            Ok(KeyParts::Ws { frame_type })
        }
        Kind::Cli => {
            let verb = key
                .strip_prefix("CLI gobby ")
                .filter(|rest| plain(rest))
                .ok_or_else(&bad)?;
            Ok(KeyParts::Cli { verb })
        }
    }
}

/// The ledger key against its case: an HTTP key names the request's method, and an MCP key names
/// the invocation's transport and tool. The invocation's own shape is checked here too.
fn check_ledger_key(
    case: &str,
    kind: Kind,
    key: &str,
    request: Option<&Value>,
    invocation: Option<&Value>,
) -> Result<(), CorpusError> {
    match parse_key(case, kind, key)? {
        KeyParts::Http { method } => {
            let requested = request
                .and_then(|request| request["method"].as_str())
                .unwrap_or_default();
            if method != requested {
                return Err(shape(
                    case,
                    format!(
                        "ledger_key method {method:?} differs from request method {requested:?}"
                    ),
                ));
            }
        }
        _ => {
            if let Some(invocation) = invocation {
                check_invocation(case, kind, key, invocation)?;
            }
        }
    }
    Ok(())
}

/// A non-HTTP invocation against its ledger key (Python `_check_invocation`): the MCP server, tool
/// and transport name the keyed ones; the WS frames carry the keyed type; the CLI argv starts with
/// the keyed verb.
fn check_invocation(
    case: &str,
    kind: Kind,
    key: &str,
    invocation: &Value,
) -> Result<(), CorpusError> {
    let object = invocation
        .as_object()
        .ok_or_else(|| shape(case, "invocation must be an object"))?;
    match parse_key(case, kind, key)? {
        KeyParts::Mcp {
            transport,
            server,
            tool,
        } => {
            let shaped = object.get("server").is_some_and(Value::is_string)
                && object.get("tool").is_some_and(Value::is_string)
                && object.get("arguments").is_some_and(Value::is_object);
            if !shaped {
                return Err(shape(
                    case,
                    "invocation needs a server, a tool and arguments",
                ));
            }
            let invoked = object
                .get("transport")
                .and_then(Value::as_str)
                .filter(|text| MCP_TRANSPORTS.contains(text))
                .ok_or_else(|| {
                    shape(
                        case,
                        format!("invocation.transport must be one of {MCP_TRANSPORTS:?}"),
                    )
                })?;
            let named = (
                object["server"].as_str().unwrap_or_default(),
                object["tool"].as_str().unwrap_or_default(),
            );
            if named != (server, tool) {
                return Err(shape(
                    case,
                    format!(
                        "invocation {}:{} differs from ledger_key {key:?}",
                        named.0, named.1
                    ),
                ));
            }
            if let Some(transport) = transport {
                let keyed = if transport == "HTTP" { "http" } else { "stdio" };
                if keyed != invoked {
                    return Err(shape(
                        case,
                        format!(
                            "ledger_key {key:?} names transport {keyed:?} but the invocation's transport is {invoked:?}"
                        ),
                    ));
                }
            }
        }
        KeyParts::Ws { frame_type } => {
            let frames = object
                .get("frames")
                .and_then(Value::as_array)
                .filter(|frames| frames.iter().all(Value::is_string))
                .ok_or_else(|| shape(case, "invocation.frames must be text frames"))?;
            for frame in frames {
                let text = frame.as_str().unwrap_or_default();
                let decoded: Value = serde_json::from_str(text)
                    .map_err(|_| shape(case, format!("frame is not JSON: {text:?}")))?;
                let decoded = decoded
                    .as_object()
                    .ok_or_else(|| shape(case, format!("frame is not a JSON object: {text:?}")))?;
                let found = decoded.get("type").and_then(Value::as_str);
                if found != Some(frame_type) {
                    return Err(shape(
                        case,
                        format!("frame type {found:?} differs from ledger_key {key:?}"),
                    ));
                }
            }
        }
        KeyParts::Cli { verb } => {
            let argv = object
                .get("argv")
                .and_then(Value::as_array)
                .filter(|argv| !argv.is_empty() && argv.iter().all(Value::is_string))
                .ok_or_else(|| shape(case, "invocation.argv must be a non-empty string list"))?;
            let first = argv[0].as_str().unwrap_or_default();
            if first != verb {
                return Err(shape(
                    case,
                    format!("argv[0] {first:?} differs from ledger_key verb {verb:?}"),
                ));
            }
            match object.get("stdin") {
                None | Some(Value::Null | Value::String(_)) => {}
                Some(_) => return Err(shape(case, "invocation.stdin must be text or null")),
            }
        }
        KeyParts::Http { .. } => {}
    }
    Ok(())
}

/// The response shape of one kind (Python `_check_response`): WS frames are text, CLI results carry
/// an exit and two streams, MCP results are JSON-RPC 2.0 messages with exactly one of `result` and
/// `error`, and HTTP answers carry an integer status.
fn check_response(case: &str, kind: Kind, response: &Value) -> Result<(), CorpusError> {
    let valid = match kind {
        Kind::Ws => response
            .as_array()
            .is_some_and(|frames| frames.iter().all(Value::is_string)),
        Kind::Cli => {
            response.get("exit").is_some_and(is_integer)
                && response.get("stdout").is_some_and(Value::is_string)
                && response.get("stderr").is_some_and(Value::is_string)
        }
        Kind::Mcp => {
            response.get("jsonrpc") == Some(&json!("2.0"))
                && response.get("id").is_some_and(is_integer)
                && response.get("result").is_some() != response.get("error").is_some()
        }
        Kind::Http => response.get("status").is_some_and(is_integer),
    };
    if valid {
        Ok(())
    } else {
        Err(shape(
            case,
            format!("response has the wrong shape for kind {}", kind_label(kind)),
        ))
    }
}

fn is_integer(value: &Value) -> bool {
    value.is_i64() || value.is_u64()
}

/// The `recorded` value of each declared entry, in declaration order.
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

/// Every case of the manifest at `path` (schema 2 or 3), each listed file parsed by [`parse_case`]
/// and checked against its name, its schema version and its manifest's families (Python
/// `_validate_case`). A schema 3 manifest must also list every case file of its directory.
pub fn load_manifest(path: &Path) -> Result<Vec<Case>, CorpusError> {
    let label = path.display().to_string();
    let manifest_error = |detail: String| CorpusError::Shape {
        case: label.clone(),
        detail,
    };
    let text = std::fs::read(path).map_err(|error| manifest_error(error.to_string()))?;
    let manifest: Value = serde_json::from_slice(&text)
        .map_err(|error| manifest_error(format!("manifest is not JSON: {error}")))?;
    let found = manifest.get("schema_version").and_then(Value::as_u64);
    let schema = match found {
        Some(schema) if ACCEPTED_SCHEMAS.contains(&schema) => schema,
        _ => return Err(CorpusError::Schema { case: label, found }),
    };
    let families = manifest
        .get("families")
        .and_then(Value::as_object)
        .ok_or_else(|| manifest_error("families must be an object".to_owned()))?;
    for (name, family) in families {
        let parity = family.get("parity").and_then(Value::as_str);
        let origin = family.get("origin").and_then(Value::as_str);
        if !parity.is_some_and(|value| PARITY_VALUES.contains(&value))
            || !origin.is_some_and(|value| ORIGIN_VALUES.contains(&value))
        {
            return Err(manifest_error(format!(
                "family {name:?} needs a parity and an origin"
            )));
        }
    }
    let listed: Vec<&str> = manifest
        .get("cases")
        .and_then(Value::as_array)
        .ok_or_else(|| manifest_error("cases must be .json file names".to_owned()))?
        .iter()
        .map(|entry| {
            entry
                .as_str()
                .filter(|name| name.ends_with(".json"))
                .ok_or_else(|| manifest_error("cases must be .json file names".to_owned()))
        })
        .collect::<Result<_, _>>()?;
    let dir = path.parent().unwrap_or_else(|| Path::new("."));
    let mut cases = Vec::with_capacity(listed.len());
    for &file in &listed {
        let value = read_case_file(&dir.join(file), file)?;
        let case = parse_case(&value)?;
        if case.schema_version != schema {
            return Err(shape(
                file,
                format!(
                    "schema_version {} differs from manifest {schema}",
                    case.schema_version
                ),
            ));
        }
        let stem = file.strip_suffix(".json").unwrap_or(file);
        if case.name != stem {
            return Err(shape(
                file,
                format!("name {:?} must equal the file stem", case.name),
            ));
        }
        if !families.contains_key(&case.family) {
            return Err(shape(
                file,
                format!("family {:?} is not in the manifest", case.family),
            ));
        }
        cases.push(case);
    }
    if schema == 3 {
        check_listing(dir, &listed)?;
    }
    Ok(cases)
}

/// Reads one case file as a JSON object.
fn read_case_file(path: &Path, file: &str) -> Result<Value, CorpusError> {
    let text = std::fs::read(path).map_err(|error| shape(file, error.to_string()))?;
    let value: Value = serde_json::from_slice(&text)
        .map_err(|error| shape(file, format!("is not JSON: {error}")))?;
    if value.is_object() {
        Ok(value)
    } else {
        Err(shape(file, "must be a JSON object"))
    }
}

/// Python `_check_listed_files`: a schema 3 directory lists every `.json` file in it. Each file
/// is the manifest, a listed case, or a declared non-case vector; any other `.json` file is refused.
fn check_listing(dir: &Path, listed: &[&str]) -> Result<(), CorpusError> {
    let label = dir.display().to_string();
    let entries = std::fs::read_dir(dir).map_err(|error| shape(&label, error.to_string()))?;
    for entry in entries {
        let entry = entry.map_err(|error| shape(&label, error.to_string()))?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if !name.ends_with(".json")
            || name == MANIFEST_FILE
            || listed.contains(&name.as_str())
            || V3_NON_CASE_FILES.contains(&name.as_str())
        {
            continue;
        }
        return Err(shape(
            &name,
            format!(
                "is a .json file in this corpus that is not listed in {MANIFEST_FILE} and is not a declared non-case vector"
            ),
        ));
    }
    Ok(())
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

/// The refusals of the Python loader (`tests/contracts/corpus_v3.py`), one rule per test.
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
        // The Python loader: "MCP stdio gobby:<tool>" names the stdio transport of its invocation.
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
        value
            .as_object_mut()
            .expect("case object")
            .shift_remove("ledger_key");
        let error = refused(&value);
        assert!(error.to_string().contains("no kind"), "{error}");
    }

    #[test]
    fn a_mcp_case_carries_no_request() {
        let value = json!({
            "schema_version": 3, "name": "rule_probe", "kind": "mcp", "family": "health",
            "backend": "up", "credential": "operator", "ledger_key": "MCP HTTP gobby:list_tools",
            "request": {"method": "POST", "path": "/api/mcp/gobby/tools/list_tools"},
            "invocation": {"transport": "http", "server": "gobby", "tool": "list_tools", "arguments": {}},
            "response": {"jsonrpc": "2.0", "id": 1, "result": {}},
            "mask": []
        });
        let error = refused(&value);
        assert!(
            error.to_string().contains("request is only for kind http"),
            "{error}"
        );
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
        assert!(
            load_manifest(&path).is_err(),
            "an unknown parity is refused"
        );
    }

    #[test]
    fn a_v3_manifest_must_list_every_case_file() {
        // Python: a case file in the v3 directory that the manifest does not list is refused.
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
    fn a_v3_manifest_ignores_a_declared_non_case_vector() {
        // grant_encoding.json is a declared vector of the v3 directory (Python V3_NON_CASE_FILES).
        let dir = tempfile::tempdir().expect("dir");
        let mut listed = http_case();
        listed["name"] = json!("a");
        write_case(dir.path(), "a.json", &listed);
        write_case(
            dir.path(),
            "grant_encoding.json",
            &json!({"schema_version": 3, "description": "x"}),
        );
        let path = write_manifest(dir.path(), 3, &["a.json"], &families());
        let cases = load_manifest(&path).expect("the declared vector is not a case");
        assert_eq!(cases.len(), 1);
        assert_eq!(cases[0].kind, Kind::Http);
    }

    #[test]
    fn a_v3_manifest_refuses_an_unlisted_json_file_that_is_not_a_declared_vector() {
        // Python: any .json file that is neither listed nor declared is refused, with or without
        // a ledger_key.
        let dir = tempfile::tempdir().expect("dir");
        let mut listed = http_case();
        listed["name"] = json!("a");
        write_case(dir.path(), "a.json", &listed);
        write_case(
            dir.path(),
            "vector.json",
            &json!({"schema_version": 3, "description": "x"}),
        );
        let path = write_manifest(dir.path(), 3, &["a.json"], &families());
        let error = load_manifest(&path).expect_err("vector.json is not declared");
        assert!(error.to_string().contains("vector.json"), "{error}");
    }

    #[test]
    fn the_shipped_v3_manifest_lists_every_case_file() {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/contracts/v3/manifest.json");
        let cases = load_manifest(&path).expect("the shipped v3 manifest loads");
        assert_eq!(cases.len(), 5);
    }
}
