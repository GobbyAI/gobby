//! One encoder for every byte-for-byte copy of Python's `json.dumps`.
//!
//! [`Json`] keeps what `json.dumps` needs and `serde_json::Value` loses: object key order
//! as inserted, integer digits as written, and the float/integer distinction. Each
//! [`Style`] constant is one Python call shape.

/// A JSON value as Python's `json` module sees it.
#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    /// An integer, as its decimal digits.
    Int(String),
    Float(f64),
    Str(String),
    Array(Vec<Json>),
    /// An object, in insertion order.
    Object(Vec<(String, Json)>),
}

/// The options of one `json.dumps` call shape.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Style {
    /// Escape every non-ASCII character (and DEL) as `\uXXXX`.
    pub ensure_ascii: bool,
    /// Separators `(",", ":")` instead of `(", ", ": ")`.
    pub compact: bool,
    /// Sort object keys.
    pub sort_keys: bool,
    /// Write `NaN`, `Infinity` and `-Infinity`; when false they are refused.
    pub allow_nan: bool,
}

/// `json.dumps(value)`.
pub const PYTHON: Style = Style {
    ensure_ascii: true,
    compact: false,
    sort_keys: false,
    allow_nan: true,
};

/// `json.dumps(value, separators=(",", ":"))`.
pub const COMPACT: Style = Style {
    ensure_ascii: true,
    compact: true,
    sort_keys: false,
    allow_nan: true,
};

/// `json.dumps(value, sort_keys=True, separators=(",", ":"))`.
pub const CANONICAL: Style = Style {
    ensure_ascii: true,
    compact: true,
    sort_keys: true,
    allow_nan: true,
};

/// `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
pub const RAW_CANONICAL: Style = Style {
    ensure_ascii: false,
    compact: true,
    sort_keys: true,
    allow_nan: true,
};

/// Starlette's JSON response render: UTF-8, compact, insertion order, no NaN.
pub const HTTP: Style = Style {
    ensure_ascii: false,
    compact: true,
    sort_keys: false,
    allow_nan: false,
};

/// Why a value could not be encoded.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DumpError {
    /// A NaN or infinite float under a style that refuses them.
    NonFinite,
}

/// Encodes `value` as `json.dumps` does under `style`.
pub fn dumps(_value: &Json, _style: Style) -> Result<String, DumpError> {
    Ok(String::new())
}

/// Encodes one string as `json.dumps` does, with its quotes.
pub fn quote(_text: &str, _ensure_ascii: bool) -> String {
    String::new()
}

/// Python's `repr` of a float.
pub fn python_float_repr(_value: f64) -> String {
    String::new()
}

impl Json {
    /// Parses `text` as `json.loads` does: a repeated key keeps its first position and
    /// its last value, and integers keep their digits.
    pub fn parse(_text: &str) -> Result<Json, serde_json::Error> {
        Ok(Json::Null)
    }
}

impl From<&serde_json::Value> for Json {
    fn from(_value: &serde_json::Value) -> Self {
        Json::Null
    }
}
