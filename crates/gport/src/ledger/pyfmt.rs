//! Python-compatible text for the values the ledger prints: `str()`, `repr()` and `json.dumps`.
//!
//! The Python checkers print these forms in their report lines and error messages, so the port
//! reproduces them byte for byte.

use serde_json::Value;

/// Python's `str()` of a parsed JSON value. A string is itself; anything else is its `repr()`.
pub fn py_str(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => py_repr(other),
    }
}

/// `str()` of an optional value; an absent key is `None`.
pub fn py_str_opt(value: Option<&Value>) -> String {
    value.map_or_else(|| "None".to_owned(), py_str)
}

/// `repr()` of an optional value; an absent key is `None`.
pub fn py_repr_opt(value: Option<&Value>) -> String {
    value.map_or_else(|| "None".to_owned(), py_repr)
}

/// Python's `str(value or "").strip()`: a falsy value becomes the empty string.
pub fn or_empty_stripped(value: Option<&Value>) -> String {
    match value {
        Some(value) if !is_falsy(value) => py_str(value).trim().to_owned(),
        _ => String::new(),
    }
}

/// Python's truthiness of a parsed JSON value.
pub fn is_falsy(value: &Value) -> bool {
    match value {
        Value::Null => true,
        Value::Bool(flag) => !flag,
        Value::Number(number) => number.as_f64() == Some(0.0),
        Value::String(text) => text.is_empty(),
        Value::Array(items) => items.is_empty(),
        Value::Object(map) => map.is_empty(),
    }
}

/// Python's `repr()` of a value as `json.loads` builds it.
pub fn py_repr(value: &Value) -> String {
    match value {
        Value::Null => "None".to_owned(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::Number(number) => py_number(number),
        Value::String(text) => py_string_repr(text),
        Value::Array(items) => {
            let parts: Vec<String> = items.iter().map(py_repr).collect();
            format!("[{}]", parts.join(", "))
        }
        Value::Object(map) => {
            let parts: Vec<String> = map
                .iter()
                .map(|(key, value)| format!("{}: {}", py_string_repr(key), py_repr(value)))
                .collect();
            format!("{{{}}}", parts.join(", "))
        }
    }
}

fn py_number(number: &serde_json::Number) -> String {
    if let Some(integer) = number.as_i64() {
        return integer.to_string();
    }
    if let Some(integer) = number.as_u64() {
        return integer.to_string();
    }
    number.as_f64().map_or_else(|| number.to_string(), py_float)
}

/// Python's `repr()` of a `str`: single quotes unless the text holds one and no double quote.
pub fn py_string_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for character in text.chars() {
        match character {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// Python's `repr()` of a float: shortest round-trip digits, fixed notation for exponents from
/// -4 to 15 and scientific notation otherwise.
fn py_float(value: f64) -> String {
    if value.is_nan() {
        return "nan".to_owned();
    }
    if value.is_infinite() {
        return if value > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    let scientific = format!("{value:e}");
    let (mantissa, exponent) = scientific
        .split_once('e')
        .unwrap_or((scientific.as_str(), "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let sign = if mantissa.starts_with('-') { "-" } else { "" };
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let body = if (-4..16).contains(&exponent) {
        fixed_notation(&digits, exponent)
    } else {
        let (first, rest) = digits.split_at(1);
        let fraction = if rest.is_empty() {
            String::new()
        } else {
            format!(".{rest}")
        };
        let marker = if exponent < 0 { '-' } else { '+' };
        format!("{first}{fraction}e{marker}{:02}", exponent.unsigned_abs())
    };
    format!("{sign}{body}")
}

/// Fixed notation of `digits` scaled by `10^exponent`, always with a fraction part.
fn fixed_notation(digits: &str, exponent: i32) -> String {
    if exponent < 0 {
        let zeros = "0".repeat(exponent.unsigned_abs() as usize - 1);
        return format!("0.{zeros}{digits}");
    }
    let split = exponent as usize + 1;
    let (integer, fraction) = if digits.len() > split {
        digits.split_at(split)
    } else {
        (digits, "")
    };
    let padding = "0".repeat(split.saturating_sub(digits.len()));
    if fraction.is_empty() {
        format!("{integer}{padding}.0")
    } else {
        format!("{integer}.{fraction}")
    }
}

/// Python's `json.dumps` of a string, with `ensure_ascii`.
pub fn json_string(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 2);
    out.push('"');
    for unit in text.encode_utf16() {
        match unit {
            0x22 => out.push_str("\\\""),
            0x5c => out.push_str("\\\\"),
            0x0a => out.push_str("\\n"),
            0x0d => out.push_str("\\r"),
            0x09 => out.push_str("\\t"),
            0x08 => out.push_str("\\b"),
            0x0c => out.push_str("\\f"),
            0x20..=0x7e => out.push(char::from(unit as u8)),
            other => out.push_str(&format!("\\u{other:04x}")),
        }
    }
    out.push('"');
    out
}

/// Python's `json.dumps` of a dict of counts, in the order given.
pub fn json_counts<'a>(entries: impl IntoIterator<Item = (&'a str, usize)>) -> String {
    let parts: Vec<String> = entries
        .into_iter()
        .map(|(key, count)| format!("{}: {count}", json_string(key)))
        .collect();
    format!("{{{}}}", parts.join(", "))
}

/// Python's `json.dumps` of a list of strings, in the order given.
pub fn json_list<'a>(items: impl IntoIterator<Item = &'a str>) -> String {
    let parts: Vec<String> = items.into_iter().map(json_string).collect();
    format!("[{}]", parts.join(", "))
}
