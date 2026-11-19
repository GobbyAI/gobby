//! One encoder for every byte-for-byte copy of Python's `json.dumps`.
//!
//! [`Json`] keeps what `json.dumps` needs and `serde_json::Value` loses: object key order
//! as inserted, integer digits as written, and the float/integer distinction. Each
//! [`Style`] constant is one Python call shape.

use std::collections::HashMap;

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
pub fn dumps(value: &Json, style: Style) -> Result<String, DumpError> {
    let mut output = String::new();
    write_value(value, style, &mut output)?;
    Ok(output)
}

fn write_value(value: &Json, style: Style, output: &mut String) -> Result<(), DumpError> {
    let (item_separator, key_separator) = if style.compact {
        (",", ":")
    } else {
        (", ", ": ")
    };
    match value {
        Json::Null => output.push_str("null"),
        Json::Bool(true) => output.push_str("true"),
        Json::Bool(false) => output.push_str("false"),
        Json::Int(digits) => output.push_str(digits),
        Json::Float(number) => output.push_str(&float_token(*number, style.allow_nan)?),
        Json::Str(text) => write_quoted(text, style.ensure_ascii, output),
        Json::Array(items) => {
            output.push('[');
            for (index, item) in items.iter().enumerate() {
                if index != 0 {
                    output.push_str(item_separator);
                }
                write_value(item, style, output)?;
            }
            output.push(']');
        }
        Json::Object(entries) => {
            let mut ordered: Vec<&(String, Json)> = entries.iter().collect();
            if style.sort_keys {
                // Rust orders strings by UTF-8 bytes, which is Python's code point order.
                ordered.sort_by(|left, right| left.0.cmp(&right.0));
            }
            output.push('{');
            for (index, (key, item)) in ordered.into_iter().enumerate() {
                if index != 0 {
                    output.push_str(item_separator);
                }
                write_quoted(key, style.ensure_ascii, output);
                output.push_str(key_separator);
                write_value(item, style, output)?;
            }
            output.push('}');
        }
    }
    Ok(())
}

/// The token `json.dumps` writes for a float: its repr, or Python's names for the
/// non-finite values when the style allows them.
fn float_token(number: f64, allow_nan: bool) -> Result<String, DumpError> {
    if number.is_finite() {
        return Ok(python_float_repr(number));
    }
    if !allow_nan {
        return Err(DumpError::NonFinite);
    }
    Ok(if number.is_nan() {
        "NaN"
    } else if number > 0.0 {
        "Infinity"
    } else {
        "-Infinity"
    }
    .to_string())
}

/// Encodes one string as `json.dumps` does, with its quotes.
pub fn quote(text: &str, ensure_ascii: bool) -> String {
    let mut output = String::with_capacity(text.len() + 2);
    write_quoted(text, ensure_ascii, &mut output);
    output
}

/// Python escapes `"`, `\` and the C0 controls always; with `ensure_ascii` it also
/// escapes everything outside printable ASCII (DEL included), as UTF-16 code units.
fn write_quoted(text: &str, ensure_ascii: bool, output: &mut String) {
    output.push('"');
    for character in text.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            '\u{8}' => output.push_str("\\b"),
            '\u{c}' => output.push_str("\\f"),
            control if u32::from(control) < 0x20 => push_units(control, output),
            other if ensure_ascii && u32::from(other) >= 0x7f => push_units(other, output),
            other => output.push(other),
        }
    }
    output.push('"');
}

fn push_units(character: char, output: &mut String) {
    for unit in character.encode_utf16(&mut [0; 2]) {
        output.push_str(&format!("\\u{unit:04x}"));
    }
}

/// Python's `repr` of a float: the shortest round-trip digits, in positional notation
/// when the decimal exponent is in `-4..16`, and in exponent notation otherwise.
pub fn python_float_repr(value: f64) -> String {
    if value.is_nan() {
        return "nan".to_string();
    }
    if value.is_infinite() {
        return if value > 0.0 { "inf" } else { "-inf" }.to_string();
    }
    let sign = if value.is_sign_negative() { "-" } else { "" };
    // `{:e}` writes the shortest digits that round-trip, as `d[.ddd]e<exponent>`.
    let scientific = format!("{:e}", value.abs());
    let (mantissa, exponent) = scientific
        .split_once('e')
        .expect("LowerExp output always carries an exponent");
    let exponent: i32 = exponent
        .parse()
        .expect("LowerExp exponent is a decimal integer");
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    if !(-4..16).contains(&exponent) {
        let fraction = if digits.len() > 1 {
            format!(".{}", &digits[1..])
        } else {
            String::new()
        };
        return format!("{sign}{}{fraction}e{exponent:+03}", &digits[..1]);
    }
    let point = exponent + 1;
    if point <= 0 {
        format!(
            "{sign}0.{}{digits}",
            "0".repeat(point.unsigned_abs() as usize)
        )
    } else {
        let point = point as usize;
        if point >= digits.len() {
            format!("{sign}{digits}{}.0", "0".repeat(point - digits.len()))
        } else {
            format!("{sign}{}.{}", &digits[..point], &digits[point..])
        }
    }
}

impl Json {
    /// Parses `text` as `json.loads` does: a repeated key keeps its first position and
    /// its last value, and integers keep their digits.
    pub fn parse(text: &str) -> Result<Json, serde_json::Error> {
        let raw: &serde_json::value::RawValue = serde_json::from_str(text)?;
        from_raw(raw.get())
    }
}

/// Builds a value from validated JSON text, reading each number from its own digits.
fn from_raw(text: &str) -> Result<Json, serde_json::Error> {
    let text = text.trim();
    match text.as_bytes().first() {
        Some(b'{') => {
            let RawEntries(raw_entries) = serde_json::from_str(text)?;
            let mut entries: Vec<(String, Json)> = Vec::with_capacity(raw_entries.len());
            let mut positions: HashMap<String, usize> = HashMap::new();
            for (key, raw) in raw_entries {
                let value = from_raw(raw.get())?;
                match positions.get(&key) {
                    Some(&position) => entries[position].1 = value,
                    None => {
                        positions.insert(key.clone(), entries.len());
                        entries.push((key, value));
                    }
                }
            }
            Ok(Json::Object(entries))
        }
        Some(b'[') => {
            let items: Vec<&serde_json::value::RawValue> = serde_json::from_str(text)?;
            items
                .into_iter()
                .map(|item| from_raw(item.get()))
                .collect::<Result<_, _>>()
                .map(Json::Array)
        }
        Some(b'"') => serde_json::from_str(text).map(Json::Str),
        Some(b't' | b'f') => serde_json::from_str(text).map(Json::Bool),
        Some(b'n') => serde_json::from_str::<()>(text).map(|()| Json::Null),
        // Rust's parser rounds correctly and, as `float()` does, gives an infinity for
        // a literal beyond the range; the text was validated as a JSON number above.
        _ if text.contains(['.', 'e', 'E']) => text
            .parse()
            .map(Json::Float)
            .map_err(serde::de::Error::custom),
        _ => Ok(Json::Int(integer_digits(text))),
    }
}

/// Python's `int` of JSON integer text: `-0` is `0`.
fn integer_digits(text: &str) -> String {
    if text
        .trim_start_matches('-')
        .bytes()
        .all(|byte| byte == b'0')
    {
        "0".to_string()
    } else {
        text.to_string()
    }
}

/// An object's members in document order, repeated keys included.
struct RawEntries<'a>(Vec<(String, &'a serde_json::value::RawValue)>);

impl<'de: 'a, 'a> serde::Deserialize<'de> for RawEntries<'a> {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Members<'a>(std::marker::PhantomData<&'a ()>);

        impl<'de: 'a, 'a> serde::de::Visitor<'de> for Members<'a> {
            type Value = RawEntries<'a>;

            fn expecting(&self, formatter: &mut std::fmt::Formatter) -> std::fmt::Result {
                formatter.write_str("a JSON object")
            }

            fn visit_map<A: serde::de::MapAccess<'de>>(
                self,
                mut map: A,
            ) -> Result<Self::Value, A::Error> {
                let mut entries = Vec::new();
                while let Some(entry) = map.next_entry()? {
                    entries.push(entry);
                }
                Ok(RawEntries(entries))
            }
        }

        deserializer.deserialize_map(Members(std::marker::PhantomData))
    }
}

impl From<&serde_json::Value> for Json {
    fn from(value: &serde_json::Value) -> Self {
        match value {
            serde_json::Value::Null => Json::Null,
            serde_json::Value::Bool(flag) => Json::Bool(*flag),
            serde_json::Value::Number(number) => match number.as_f64() {
                Some(float) if number.is_f64() => Json::Float(float),
                _ => Json::Int(number.to_string()),
            },
            serde_json::Value::String(text) => Json::Str(text.clone()),
            serde_json::Value::Array(items) => Json::Array(items.iter().map(Json::from).collect()),
            serde_json::Value::Object(entries) => Json::Object(
                entries
                    .iter()
                    .map(|(key, item)| (key.clone(), Json::from(item)))
                    .collect(),
            ),
        }
    }
}
